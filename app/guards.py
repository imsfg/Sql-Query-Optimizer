"""Cheap deterministic checks on raw user input, run before any LLM call.

These are a first line of defence only; the LLM classifier, the SQL validator and the
read-only database connection each enforce the same policies independently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_INJECTION_PATTERNS = [
    r"\bignore\s+(?:all\s+|any\s+|the\s+|your\s+)*(?:previous|prior|above|earlier|preceding|system)\s+"
    r"(?:instructions?|rules|prompts?|directions|messages?)",
    r"\bignore\s+(?:all\s+|any\s+|the\s+|your\s+)+(?:rules|instructions|guidelines|restrictions|guardrails)\b",
    r"\bdisregard\s+(?:all\s+|any\s+|the\s+|your\s+)*(?:previous|prior|above|system|instructions?|rules)",
    r"\bforget\s+(?:all\s+|your\s+|the\s+|previous\s+)*(?:instructions?|rules|guidelines)",
    r"\b(?:reveal|show|print|repeat|output|display|leak)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+|hidden\s+|initial\s+)?"
    r"(?:prompt|instructions|rules)",
    r"\bsystem\s+prompt\b",
    r"\byou\s+are\s+(?:now|no\s+longer)\b",
    r"\bpretend\s+(?:you\s+are|to\s+be)\b",
    r"\b(?:developer|dan|jailbreak|god|admin|unrestricted)\s+mode\b",
    r"\bnew\s+(?:system\s+)?instructions?\s*:",
    r"\boverride\s+(?:your|the|all|any)\s+(?:rules|instructions|guardrails|restrictions|safety)",
    r"</?\s*(?:system|instructions?|user_message|user_sql|schema|conversation)\s*>",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.I)

# Statement-shaped write/DDL/admin SQL. Natural-language requests ("delete old orders")
# are left to the LLM classifier, since phrasings like "update the previous query" are fine.
_DESTRUCTIVE_SQL_RE = re.compile(
    r"\b(?:"
    r"DELETE\s+FROM"
    r"|DROP\s+(?:TABLE|DATABASE|VIEW|INDEX|TRIGGER|SCHEMA)"
    r"|TRUNCATE\s+(?:TABLE\s+)?\w"
    r"|ALTER\s+(?:TABLE|DATABASE|VIEW)"
    r"|INSERT\s+(?:OR\s+\w+\s+)?INTO"
    r"|REPLACE\s+INTO"
    r"|UPDATE\s+[\w\"`\[\]]+\s+SET"
    r"|CREATE\s+(?:TABLE|VIEW|TRIGGER|DATABASE|SCHEMA)"
    r"|MERGE\s+INTO"
    r"|(?:GRANT|REVOKE)\s+\w+"
    r"|(?:ATTACH|DETACH)\s+DATABASE"
    r"|PRAGMA\s+\w+\s*="
    r"|VACUUM\b"
    r")",
    re.I,
)

_FENCE_RE = re.compile(r"```(?:sql|sqlite)?\s*\n?(.*?)```", re.S | re.I)
_SQL_START_RE = re.compile(r"(?im)^\s*(SELECT|WITH)\b")
_SQL_SIGNAL_RE = re.compile(r"[*,=();.<>]")


@dataclass
class GuardResult:
    blocked: bool
    reason: str | None = None  # "empty" | "too_long" | "injection" | "destructive"
    detail: str | None = None
    user_sql: str | None = None


def check_input(text: str, max_chars: int) -> GuardResult:
    stripped = (text or "").strip()
    if not stripped:
        return GuardResult(True, "empty")
    if len(stripped) > max_chars:
        return GuardResult(True, "too_long", f"{len(stripped)} characters (limit {max_chars})")
    m = _INJECTION_RE.search(stripped)
    if m:
        return GuardResult(True, "injection", m.group(0))
    m = _DESTRUCTIVE_SQL_RE.search(stripped)
    if m:
        return GuardResult(True, "destructive", " ".join(m.group(0).split()[:2]).upper())
    return GuardResult(False, user_sql=extract_sql(stripped))


def extract_sql(text: str) -> str | None:
    """Pull a SQL query out of a message: a fenced code block, or a line that starts with
    SELECT/WITH and has SQL punctuation. "Select all customers from Texas" is plain English,
    so it is deliberately not treated as SQL."""
    fence = _FENCE_RE.search(text)
    if fence and fence.group(1).strip():
        return fence.group(1).strip()
    m = _SQL_START_RE.search(text)
    if not m:
        return None
    candidate = text[m.start():].strip()
    if re.search(r"\bFROM\b", candidate, re.I) and _SQL_SIGNAL_RE.search(candidate):
        return candidate
    return None
