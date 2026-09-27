"""Runtime settings, read from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_MODELS = {"anthropic": "claude-opus-5", "gemini": "gemini-3.8-flash"}


def _default_provider() -> str:
    """LLM_PROVIDER if set; otherwise whichever provider has a key (Anthropic first)."""
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if not os.getenv("ANTHROPIC_API_KEY") and (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        return "gemini"
    return "anthropic"


@dataclass(frozen=True)
class Settings:
    llm_provider: str = field(default_factory=_default_provider)  # "anthropic" | "gemini"
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", ""))  # "" = provider default
    # Server-side refusal fallback (Anthropic beta). Disable if your gateway rejects beta params.
    llm_refusal_fallback: bool = field(default_factory=lambda: _bool("LLM_REFUSAL_FALLBACK", True))
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_S", "90")))

    db_path: Path = field(default_factory=lambda: Path(os.getenv("DB_PATH", ROOT_DIR / "data" / "company.db")))
    checkpoint_db_path: Path = field(
        default_factory=lambda: Path(os.getenv("CHECKPOINT_DB_PATH", ROOT_DIR / "data" / "checkpoints.db"))
    )

    max_sql_attempts: int = field(default_factory=lambda: int(os.getenv("MAX_SQL_ATTEMPTS", "3")))
    max_result_rows: int = field(default_factory=lambda: int(os.getenv("MAX_RESULT_ROWS", "200")))
    max_export_rows: int = field(default_factory=lambda: int(os.getenv("MAX_EXPORT_ROWS", "10000")))
    query_timeout_s: float = field(default_factory=lambda: float(os.getenv("QUERY_TIMEOUT_S", "5")))
    max_input_chars: int = field(default_factory=lambda: int(os.getenv("MAX_INPUT_CHARS", "4000")))
    history_turns: int = field(default_factory=lambda: int(os.getenv("HISTORY_TURNS", "8")))
    # Above this many tables, only the relevant slice of the schema is sent to the LLM.
    full_schema_table_limit: int = field(default_factory=lambda: int(os.getenv("FULL_SCHEMA_TABLE_LIMIT", "15")))

    def __post_init__(self) -> None:
        if self.llm_provider not in DEFAULT_MODELS:
            raise ValueError(f"LLM_PROVIDER must be one of {sorted(DEFAULT_MODELS)}, got '{self.llm_provider}'")
        # An empty model, or one that belongs to the other provider (e.g. LLM_MODEL=claude-opus-5
        # left over after switching to Gemini), falls back to the provider's default.
        prefix = "gemini" if self.llm_provider == "gemini" else "claude"
        if not self.llm_model.startswith(prefix):
            object.__setattr__(self, "llm_model", DEFAULT_MODELS[self.llm_provider])


settings = Settings()
