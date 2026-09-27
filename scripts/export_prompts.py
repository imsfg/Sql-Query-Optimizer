"""Regenerate docs/PROMPTS.md from app/prompts.py so the documented prompts never drift.

Usage:  python -m scripts.export_prompts
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import prompts  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SchemaCatalog  # noqa: E402
from data.seed import build_database  # noqa: E402

SECTIONS = [
    ("System prompt (shared by every call, prompt-cached)", "SYSTEM_PROMPT",
     "Role, the five non-negotiable rules, and the annotated schema. Byte-identical across calls, so "
     "Anthropic prompt caching applies; per-task instructions go in the user turn instead."),
    ("Intent classification + scope validation", "CLASSIFY_TASK",
     "Node `classify_intent`, effort `low`. Structured output: `IntentDecision`."),
    ("SQL generation (new questions and follow-ups)", "GENERATE_TASK",
     "Node `generate_sql`, effort `medium`. Structured output: `GeneratedSQL`."),
    ("SQL debugging", "DEBUG_TASK",
     "Node `sql_task` (intent=debug). The validator's and SQLite's errors are injected as evidence. "
     "Structured output: `DebugResult`."),
    ("SQL optimization", "OPTIMIZE_TASK",
     "Node `sql_task` (intent=optimize). The EXPLAIN QUERY PLAN analysis is injected as evidence. "
     "Structured output: `OptimizeResult`."),
    ("SQL explanation", "EXPLAIN_TASK", "Node `sql_task` (intent=explain), effort `low`. Structured output: `ExplainResult`."),
    ("Retry feedback (appended on validation failure)", "RETRY_FEEDBACK",
     "Appended to the generate/debug/optimize prompt when the previous attempt failed validation."),
]


def main() -> None:
    build_database(settings.db_path)
    schema = SchemaCatalog.from_sqlite(settings.db_path).prompt_text()
    out = [
        "# Prompts",
        "",
        "_Generated from [`app/prompts.py`](../app/prompts.py) by `python -m scripts.export_prompts`. Do not edit by hand._",
        "",
        "Every call returns **structured output** (a Pydantic model, enforced by the API's JSON-schema output), "
        "so nodes never parse free text. User-controlled text is always wrapped in tags "
        "(`<user_message>`, `<user_sql>`, `<conversation>`, `<previous_sql>`), and the system prompt declares it untrusted.",
        "",
    ]
    for title, name, note in SECTIONS:
        text = getattr(prompts, name)
        out += [f"## {title}", "", note, "", "```text", text.rstrip(), "```", ""]
    out += ["## Rendered schema block", "", "What `{schema}` expands to for the sample database:", "",
            "```sql", schema, "```", ""]
    path = ROOT / "docs" / "PROMPTS.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(out))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
