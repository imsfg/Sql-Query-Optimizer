"""Graph state.

`history` and `last_sql` persist across turns through the checkpointer (one thread per
conversation). Everything else is per-turn working state and is reset by the first node.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class Turn(TypedDict, total=False):
    role: str  # "user" | "assistant"
    content: str
    sql: str | None


class Step(TypedDict):
    node: str
    label: str
    detail: str


class AgentState(TypedDict, total=False):
    # conversation memory
    history: Annotated[list[Turn], operator.add]
    last_sql: str | None

    # turn input
    user_input: str
    execute: bool

    # routing
    intent: str | None
    block_reason: str | None     # set by guards: empty | too_long | injection | destructive
    block_detail: str | None
    clarification_question: str | None

    # SQL pipeline
    user_sql: str | None         # SQL supplied by the user (optimize / debug / explain / modify base)
    user_validation: dict | None
    user_performance: dict | None
    relevant_tables: list[str]
    schema_context: str
    producer: str | None         # node that produced candidate_sql; retries loop back to it
    candidate_sql: str | None
    attempts: int
    validation: dict | None
    llm_output: dict | None
    performance: dict | None
    execution: dict | None
    equivalence: dict | None

    # output
    trace: list[Step]
    response: dict[str, Any] | None


def turn_defaults() -> dict[str, Any]:
    return {
        "intent": None, "block_reason": None, "block_detail": None, "clarification_question": None,
        "user_sql": None, "user_validation": None, "user_performance": None, "relevant_tables": [],
        "schema_context": "", "producer": None, "candidate_sql": None, "attempts": 0, "validation": None,
        "llm_output": None, "performance": None, "execution": None, "equivalence": None,
        "trace": [], "response": None,
    }
