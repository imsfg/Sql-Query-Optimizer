"""A scripted stand-in for the LLM so graph tests are deterministic and offline."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.llm import LLMError


class FakeLLM:
    """Returns queued responses per task ("classify", "generate", "debug", "optimize", "explain").

    A queued item may be a Pydantic object, a dict (validated into the requested schema), a
    callable taking the prompt, or an exception to raise. Every call is recorded in `calls`."""

    def __init__(self, script: dict[str, list[Any]] | None = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, system: str, prompt: str, schema: type[BaseModel], effort: str, task: str):
        self.calls.append({"task": task, "prompt": prompt, "system": system, "effort": effort})
        queue = self.script.get(task)
        if not queue:
            raise AssertionError(f"FakeLLM has no scripted response for task '{task}'")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, Callable) and not isinstance(item, (BaseModel, dict)):
            item = item(prompt)
        return item if isinstance(item, BaseModel) else schema.model_validate(item)

    def tasks(self) -> list[str]:
        return [c["task"] for c in self.calls]


def intent(name: str, user_sql: str | None = None, question: str | None = None) -> dict:
    return {"intent": name, "user_sql": user_sql, "clarification_question": question, "reason": "test"}


def generated(sql: str, explanation: str = "Test explanation.", answerable: bool = True, message: str = "") -> dict:
    return {"answerable": answerable, "sql": sql, "explanation": explanation, "assumptions": [], "message": message}


__all__ = ["FakeLLM", "LLMError", "intent", "generated"]
