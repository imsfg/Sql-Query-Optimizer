"""LLM access: one method that returns a validated Pydantic object (structured outputs).

Graph nodes depend on the `StructuredLLM` protocol, not on a vendor SDK, so providers are
interchangeable (Claude or Gemini, chosen by LLM_PROVIDER) and tests can inject a scripted fake.
"""

from __future__ import annotations

import copy
import logging
import os
from typing import Any, Literal, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
Effort = Literal["low", "medium", "high"]


class LLMError(RuntimeError):
    """The model call failed. The message is safe to show to end users."""


# ---- structured output schemas -----------------------------------------------------------

Intent = Literal["generate", "modify", "explain", "optimize", "debug", "schema_info", "help",
                 "destructive", "out_of_scope", "clarify"]


class IntentDecision(BaseModel):
    intent: Intent
    user_sql: str | None = Field(None, description="SQL pasted by the user, verbatim; null if none")
    clarification_question: str | None = Field(None, description="Only for intent=clarify")
    reason: str = Field(description="One short sentence justifying the intent")


class GeneratedSQL(BaseModel):
    answerable: bool
    sql: str
    explanation: str
    assumptions: list[str]
    message: str = Field(description="When answerable=false: what is missing and what could be answered")


class Issue(BaseModel):
    problem: str
    why: str
    fix: str


class DebugResult(BaseModel):
    issues: list[Issue]
    corrected_sql: str
    explanation: str


class IndexRecommendation(BaseModel):
    ddl: str = Field(description="A CREATE INDEX statement")
    reason: str


class OptimizeResult(BaseModel):
    optimized_sql: str
    changes: list[str]
    index_recommendations: list[IndexRecommendation]
    explanation: str


class ExplainResult(BaseModel):
    explanation: str
    steps: list[str]


# ---- clients ------------------------------------------------------------------------------

class StructuredLLM(Protocol):
    def generate(self, *, system: str, prompt: str, schema: type[T], effort: Effort, task: str) -> T: ...


class AnthropicLLM:
    """Claude via the official SDK. The system prompt (rules + schema) is identical on every
    call and marked for prompt caching."""

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: Settings):
        self.model = settings.llm_model
        self.refusal_fallback = settings.llm_refusal_fallback
        self.client = anthropic.Anthropic(timeout=settings.llm_timeout_s, max_retries=2)

    def generate(self, *, system: str, prompt: str, schema: type[T], effort: Effort, task: str) -> T:
        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            output_config={"effort": effort},
        )
        try:
            if self.refusal_fallback:
                # If a safety classifier declines, the API retries on Anthropic's recommended model.
                response = self.client.beta.messages.parse(
                    betas=[self.FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                response = self.client.messages.parse(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError("The AI service rejected the credentials. Check ANTHROPIC_API_KEY.") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError("The API key is not allowed to use this model.") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Model '{self.model}' was not found. Check LLM_MODEL.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("The AI service is rate-limiting requests. Please try again shortly.") from e
        except anthropic.BadRequestError as e:
            log.warning("LLM bad request (%s): %s", task, e.message)
            raise LLMError(f"The AI service rejected the request: {e.message}") from e
        except anthropic.APIStatusError as e:
            log.warning("LLM status %s (%s): %s", e.status_code, task, e.message)
            raise LLMError("The AI service returned an error. Please try again.") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the AI service. Check your network connection.") from e
        except anthropic.AnthropicError as e:  # e.g. no credentials configured
            log.warning("LLM client error (%s): %s", task, e)
            raise LLMError("The AI service is not configured. Set ANTHROPIC_API_KEY and restart.") from e

        usage = response.usage
        log.info("llm task=%s stop=%s in=%s cached=%s out=%s", task, response.stop_reason, usage.input_tokens,
                 usage.cache_read_input_tokens, usage.output_tokens)
        if response.stop_reason == "refusal":
            raise LLMError("The AI model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError("The AI response was cut off. Please try a more specific request.")
        if response.parsed_output is None:
            raise LLMError("The AI response could not be parsed.")
        return response.parsed_output


class GeminiLLM:
    """Gemini via the google-genai SDK, using JSON-schema constrained output."""

    # Gemini 3.x takes a thinking level; 2.5 models take a token budget.
    _BUDGETS = {"low": 512, "medium": 2048, "high": 8192}

    def __init__(self, settings: Settings):
        self.model = settings.llm_model
        # Used when the primary model stays overloaded (503) after retries.
        fallback = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.7-flash").strip()
        self.models = [self.model] + ([fallback] if fallback and fallback != self.model else [])
        self.timeout_ms = int(settings.llm_timeout_s * 1000)
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not key:
                raise LLMError("The AI service is not configured. Set GEMINI_API_KEY and restart.")
            retry = types.HttpRetryOptions(attempts=3, initial_delay=1.0, max_delay=8.0,
                                           http_status_codes=[429, 500, 502, 503, 504])
            self._client = genai.Client(api_key=key, http_options=types.HttpOptions(
                timeout=self.timeout_ms, retry_options=retry))
        return self._client

    def _thinking(self, model: str, effort: Effort):
        from google.genai import types

        if model.startswith("gemini-2"):
            return types.ThinkingConfig(thinking_budget=self._BUDGETS[effort])
        return types.ThinkingConfig(thinking_level=effort.upper())

    def _call(self, client, model: str, system: str, prompt: str, schema: type[BaseModel], effort: Effort):
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=inline_schema_refs(schema.model_json_schema()),
            thinking_config=self._thinking(model, effort),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        return client.models.generate_content(model=model, contents=prompt, config=config)

    def generate(self, *, system: str, prompt: str, schema: type[T], effort: Effort, task: str) -> T:
        from google.genai import errors

        client = self._get_client()
        try:
            for i, model in enumerate(self.models):
                try:
                    response = self._call(client, model, system, prompt, schema, effort)
                    break
                except errors.ServerError as e:
                    if e.code != 503 or i == len(self.models) - 1:
                        raise
                    log.warning("Gemini %s overloaded (%s); falling back to %s", model, task, self.models[i + 1])
        except errors.ClientError as e:
            log.warning("Gemini client error %s (%s): %s", e.code, task, e.message)
            if e.code == 429:
                raise LLMError("Gemini quota or rate limit reached. Wait a minute and try again, "
                               "or enable billing on the API key.") from e
            if e.code in (401, 403) or "API key" in (e.message or ""):
                raise LLMError("Gemini rejected the API key. Check GEMINI_API_KEY.") from e
            if e.code == 404:
                raise LLMError(f"Gemini model '{self.model}' was not found. Check LLM_MODEL.") from e
            raise LLMError(f"Gemini rejected the request: {e.message}") from e
        except errors.ServerError as e:
            log.warning("Gemini server error %s (%s): %s", e.code, task, e.message)
            if e.code == 503:
                raise LLMError("Gemini is overloaded right now (high demand). Please try again in a moment.") from e
            raise LLMError("The Gemini service returned an error. Please try again.") from e
        except errors.APIError as e:
            raise LLMError(f"Gemini error: {e.message}") from e
        except Exception as e:  # noqa: BLE001 - network failures surface as transport exceptions
            log.warning("Gemini call failed (%s): %r", task, e)
            raise LLMError("Could not reach the Gemini service. Check your network connection.") from e

        usage = response.usage_metadata
        if usage:
            log.info("llm task=%s in=%s cached=%s out=%s", task, usage.prompt_token_count,
                     usage.cached_content_token_count, usage.candidates_token_count)
        text = response.text
        if not text:
            reason = response.candidates[0].finish_reason if response.candidates else "blocked"
            raise LLMError(f"Gemini returned no answer (finish reason: {reason}).")
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            log.warning("Gemini output failed schema validation (%s): %s", task, e)
            raise LLMError("The AI response could not be parsed.") from e


def inline_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace $ref/$defs with the referenced definitions, since not every provider's schema
    dialect resolves references."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
                return resolve({**target, **{k: v for k, v in node.items() if k != "$ref"}})
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def make_llm(settings: Settings) -> StructuredLLM:
    if settings.llm_provider == "gemini":
        return GeminiLLM(settings)
    return AnthropicLLM(settings)
