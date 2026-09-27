"""LangGraph workflow wiring and a small facade used by the API.

    START → guard_input ─┬─(blocked)────────────────────────────────────────────► refuse ──┐
                         └─► classify_intent ─┬─(out_of_scope / destructive)────► refuse ──┤
                                              ├─(help / schema_info / clarify)──► ... ─────┤
                                              └─► retrieve_schema                           │
                                                   ├─(generate/modify)─► generate_sql ──┐   │
                                                   └─► inspect_user_sql ─► sql_task ────┤   │
                                                        (debug/optimize/explain)        ▼   │
                                   ┌──── retry with validator feedback ──── validate_sql    │
                                   ▼                                         │ ok  │ fail   │
                          generate_sql / sql_task          analyze_performance ◄──┘ fail ───┤
                                                           → execute_sql (optional)         │
                                                           → generate_explanation ──────────┤
                                                                                  finalize ◄┘ → END
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import LABELS, Deps, Nodes
from app.agent.state import AgentState


def build_graph(deps: Deps, checkpointer=None):
    n = Nodes(deps)
    g = StateGraph(AgentState)
    for name in ("guard_input", "classify_intent", "retrieve_schema", "generate_sql", "inspect_user_sql",
                 "sql_task", "validate_sql", "fail", "analyze_performance", "execute_sql", "generate_explanation",
                 "refuse", "help", "schema_info", "clarify", "finalize"):
        g.add_node(name, getattr(n, name))

    g.add_edge(START, "guard_input")
    g.add_conditional_edges("guard_input", n.route_after_guard, ["refuse", "classify_intent"])
    g.add_conditional_edges("classify_intent", n.route_after_classify,
                            ["retrieve_schema", "refuse", "help", "schema_info", "clarify", "finalize"])
    g.add_conditional_edges("retrieve_schema", n.route_after_retrieve, ["generate_sql", "inspect_user_sql"])
    g.add_conditional_edges("inspect_user_sql", n.route_after_inspect, ["sql_task", "refuse"])
    g.add_conditional_edges("generate_sql", n.route_after_producer, ["validate_sql", "finalize"])
    g.add_conditional_edges("sql_task", n.route_after_producer, ["validate_sql", "finalize"])
    g.add_conditional_edges("validate_sql", n.route_after_validate,
                            ["analyze_performance", "generate_sql", "sql_task", "fail"])
    g.add_conditional_edges("fail", n.route_after_fail, ["analyze_performance", "finalize"])
    g.add_conditional_edges("analyze_performance", n.route_after_analyze, ["execute_sql", "generate_explanation"])
    g.add_edge("execute_sql", "generate_explanation")
    for terminal in ("generate_explanation", "refuse", "help", "schema_info", "clarify"):
        g.add_edge(terminal, "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


class SQLAgent:
    """Runs one conversational turn per call; conversation memory lives in the checkpointer,
    keyed by thread_id."""

    def __init__(self, deps: Deps, checkpointer=None):
        self.graph = build_graph(deps, checkpointer)

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}

    def run(self, thread_id: str, message: str, execute: bool = True) -> dict[str, Any]:
        state = self.graph.invoke({"user_input": message, "execute": execute}, self._config(thread_id))
        return state["response"]

    def stream(self, thread_id: str, message: str, execute: bool = True) -> Iterator[dict[str, Any]]:
        """Yields progress events ({"event": "step", ...}) as nodes start and finish, then a
        final {"event": "result", "response": ...}."""
        config = self._config(thread_id)
        for chunk in self.graph.stream({"user_input": message, "execute": execute}, config, stream_mode="tasks"):
            name = chunk.get("name")
            if name not in LABELS or name == "finalize":
                continue
            if "input" in chunk:  # task started
                yield {"event": "step", "node": name, "label": LABELS[name], "status": "running"}
            else:  # task finished
                trace = _result_value(chunk.get("result"), "trace") or []
                last = trace[-1] if trace else {"label": LABELS[name], "detail": ""}
                yield {"event": "step", "node": name, "label": last["label"], "detail": last["detail"],
                       "status": "error" if chunk.get("error") else "done"}
        yield {"event": "result", "response": self.graph.get_state(config).values.get("response")}

    def history(self, thread_id: str) -> list[dict[str, Any]]:
        return self.graph.get_state(self._config(thread_id)).values.get("history", [])

    def mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()


def _result_value(result: Any, key: str) -> Any:
    """Task results arrive as a dict or as a list of (channel, value) writes depending on version."""
    if isinstance(result, dict):
        return result.get(key)
    if isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == key:
                return item[1]
    return None
