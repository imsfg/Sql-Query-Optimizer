"""LangGraph nodes and routing functions.

Each node returns a partial state update and appends one entry to `trace`, which is streamed
to the UI as live progress. Nodes that end a turn set `response`; every path goes through
`finalize`, which records the turn in conversation memory.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from app import prompts
from app.agent.state import AgentState, turn_defaults
from app.config import Settings
from app.database import SchemaCatalog, execute_readonly
from app.guards import check_input
from app.llm import (DebugResult, ExplainResult, GeneratedSQL, IntentDecision, LLMError, OptimizeResult,
                     StructuredLLM)
from app.sql_analysis import analyze_performance
from app.sql_validator import clean_sql, transpile, validate_sql

log = logging.getLogger(__name__)

OUT_OF_SCOPE_MESSAGE = ("I'm designed to assist only with SQL and database-related tasks. "
                        "Please ask a question related to the provided database schema.")

LABELS = {
    "guard_input": "Checking request",
    "classify_intent": "Detecting intent",
    "retrieve_schema": "Retrieving schema",
    "generate_sql": "Generating SQL",
    "inspect_user_sql": "Inspecting your SQL",
    "sql_task": "Analyzing SQL",
    "validate_sql": "Validating SQL",
    "fail": "Handling validation failure",
    "analyze_performance": "Optimizing / estimating cost",
    "execute_sql": "Executing query",
    "generate_explanation": "Generating explanation",
    "refuse": "Declining request",
    "help": "Preparing help",
    "schema_info": "Describing schema",
    "clarify": "Asking for clarification",
    "finalize": "Saving conversation",
}

SQL_INTENTS = {"generate", "modify", "explain", "optimize", "debug"}


@dataclass
class Deps:
    settings: Settings
    catalog: SchemaCatalog
    llm: StructuredLLM


class Nodes:
    def __init__(self, deps: Deps):
        self.s = deps.settings
        self.catalog = deps.catalog
        self.llm = deps.llm
        self.small_schema = len(self.catalog.tables) <= self.s.full_schema_table_limit
        self.full_schema = self.catalog.prompt_text()

    # ---- helpers ----------------------------------------------------------------------
    @staticmethod
    def _step(state: AgentState, node: str, detail: str = "") -> dict[str, Any]:
        return {"trace": list(state.get("trace") or []) + [{"node": node, "label": LABELS[node], "detail": detail}]}

    def _system(self, state: AgentState | None) -> str:
        schema = (state or {}).get("schema_context") or self._classifier_schema()
        return prompts.SYSTEM_PROMPT.format(schema=schema)

    def _classifier_schema(self) -> str:
        if self.small_schema:
            return self.full_schema
        return "\n".join(f"{t.name}({', '.join(c.name for c in t.columns)})" for t in self.catalog.tables.values())

    def _context(self, state: AgentState, previous_sql: str | None = None, note_missing: bool = False) -> str:
        return prompts.render_context(state.get("history") or [], previous_sql, self.s.history_turns, note_missing)

    def _feedback(self, state: AgentState) -> str:
        v = state.get("validation")
        if not state.get("attempts") or not v or v.get("ok"):
            return ""
        return prompts.RETRY_FEEDBACK.format(sql=v["sql"], errors="\n".join(f"- {e}" for e in v["errors"]))

    def _error(self, state: AgentState, node: str, message: str) -> dict[str, Any]:
        return {"response": {"type": "error", "intent": state.get("intent"), "message": message},
                **self._step(state, node, f"Error: {message}")}

    # ---- 1. guard -------------------------------------------------------------------
    def guard_input(self, state: AgentState) -> dict[str, Any]:
        update = turn_defaults()
        g = check_input(state.get("user_input", ""), self.s.max_input_chars)
        if g.blocked:
            update.update(block_reason=g.reason, block_detail=g.detail,
                          intent="destructive" if g.reason == "destructive" else None)
            detail = {"empty": "Empty message", "too_long": f"Message too long: {g.detail}",
                      "injection": f"Possible prompt injection: \"{g.detail}\"",
                      "destructive": f"Write operation detected: {g.detail}"}[g.reason]
        else:
            update["user_sql"] = g.user_sql
            detail = "No injection or write operations found" + ("; SQL found in message" if g.user_sql else "")
        update["trace"] = [{"node": "guard_input", "label": LABELS["guard_input"], "detail": detail}]
        return update

    @staticmethod
    def route_after_guard(state: AgentState) -> str:
        return "refuse" if state.get("block_reason") else "classify_intent"

    # ---- 2. intent detection + scope validation ------------------------------------------
    def classify_intent(self, state: AgentState) -> dict[str, Any]:
        prompt = prompts.CLASSIFY_TASK.format(
            context=self._context(state, state.get("last_sql"), note_missing=True), message=state["user_input"])
        try:
            decision = self.llm.generate(system=self._system(None), prompt=prompt, schema=IntentDecision,
                                         effort="low", task="classify")
        except LLMError as e:
            return self._error(state, "classify_intent", str(e))

        intent = decision.intent
        user_sql = state.get("user_sql") or (clean_sql(decision.user_sql) if (decision.user_sql or "").strip() else None)
        last_sql = state.get("last_sql")
        question = decision.clarification_question
        notes = [decision.reason]

        if intent == "generate" and user_sql:
            intent = "modify"  # "take this query and ..." builds on the pasted SQL
        if intent == "modify" and not (user_sql or last_sql):
            intent = "generate"
            notes.append("no previous query, treating as a new question")
        if intent in ("explain", "optimize", "debug") and not user_sql:
            if last_sql:
                user_sql = last_sql
                notes.append("using the previous query")
            else:
                question = f"Please paste the SQL query you would like me to {intent}."
                intent = "clarify"
        if intent == "clarify" and not question:
            question = ("Could you be more specific? For example, which data (customers, orders, products, "
                        "employees), which filters, and which time period?")

        return {"intent": intent, "user_sql": user_sql, "clarification_question": question,
                **self._step(state, "classify_intent", f"{intent}: {'; '.join(notes)}")}

    @staticmethod
    def route_after_classify(state: AgentState) -> str:
        if state.get("response"):
            return "finalize"
        intent = state.get("intent")
        if intent in SQL_INTENTS:
            return "retrieve_schema"
        return {"help": "help", "schema_info": "schema_info", "clarify": "clarify"}.get(intent, "refuse")

    # ---- 3. schema retrieval --------------------------------------------------------------
    def retrieve_schema(self, state: AgentState) -> dict[str, Any]:
        tables = self.catalog.relevant_tables(state["user_input"], state.get("user_sql") or state.get("last_sql"))
        if self.small_schema:
            context = self.full_schema
            detail = f"Relevant: {', '.join(tables)} (full {len(self.catalog.tables)}-table schema provided)"
        else:
            context = self.catalog.prompt_text(tables)
            detail = f"Provided {len(tables)} of {len(self.catalog.tables)} tables: {', '.join(tables)}"
        return {"relevant_tables": tables, "schema_context": context, **self._step(state, "retrieve_schema", detail)}

    @staticmethod
    def route_after_retrieve(state: AgentState) -> str:
        return "generate_sql" if state.get("intent") in ("generate", "modify") else "inspect_user_sql"

    # ---- 4a. SQL generation ------------------------------------------------------------------
    def generate_sql(self, state: AgentState) -> dict[str, Any]:
        previous = (state.get("user_sql") or state.get("last_sql")) if state.get("intent") == "modify" else None
        prompt = prompts.GENERATE_TASK.format(context=self._context(state, previous), message=state["user_input"],
                                              feedback=self._feedback(state))
        try:
            out = self.llm.generate(system=self._system(state), prompt=prompt, schema=GeneratedSQL,
                                    effort="medium", task="generate")
        except LLMError as e:
            return self._error(state, "generate_sql", str(e))
        attempts = (state.get("attempts") or 0) + 1
        if not out.answerable or not out.sql.strip():
            message = out.message or "The database schema does not contain the data needed to answer that."
            return {"attempts": attempts, "response": {"type": "info", "intent": state.get("intent"), "message": message},
                    **self._step(state, "generate_sql", "Not answerable from this schema")}
        return {"candidate_sql": clean_sql(out.sql), "llm_output": out.model_dump(), "attempts": attempts,
                "producer": "generate_sql", **self._step(state, "generate_sql", f"Attempt {attempts}")}

    # ---- 4b. user-supplied SQL: inspect, then debug / optimize / explain ---------------------
    def inspect_user_sql(self, state: AgentState) -> dict[str, Any]:
        v = validate_sql(state["user_sql"], self.catalog, self.s.db_path)
        update: dict[str, Any] = {"user_validation": v.to_dict()}
        intent = state["intent"]
        if not v.read_only:
            update.update(intent="destructive", block_detail=v.errors[-1].rsplit("found ", 1)[-1].rstrip("."))
            detail = "Query modifies data or schema"
        elif not v.ok and intent in ("optimize", "explain"):
            update["intent"] = "debug"
            detail = f"Query has {len(v.errors)} error(s), so switching to debugging"
        elif v.ok:
            perf = analyze_performance(v.sql, self.catalog, v.plan)
            update["user_performance"] = perf.to_dict()
            detail = f"Valid query; estimated cost {perf.cost_level}"
        else:
            detail = f"{len(v.errors)} error(s) found: {v.errors[0]}"
        return {**update, **self._step(state, "inspect_user_sql", detail)}

    @staticmethod
    def route_after_inspect(state: AgentState) -> str:
        return "refuse" if state.get("intent") == "destructive" else "sql_task"

    def sql_task(self, state: AgentState) -> dict[str, Any]:
        intent, sql = state["intent"], state["user_sql"]
        context = self._context(state)
        feedback = self._feedback(state)
        try:
            if intent == "debug":
                uv = state.get("user_validation") or {}
                findings = "\n".join(f"- {e}" for e in uv.get("errors", []) + uv.get("warnings", [])) or \
                    "- No automated errors: the query runs, so look for logic problems."
                out = self.llm.generate(system=self._system(state), schema=DebugResult, effort="medium", task="debug",
                                        prompt=prompts.DEBUG_TASK.format(findings=findings, context=context, sql=sql,
                                                                         message=state["user_input"], feedback=feedback))
                candidate, detail = out.corrected_sql, f"{len(out.issues)} issue(s) identified"
            elif intent == "optimize":
                out = self.llm.generate(system=self._system(state), schema=OptimizeResult, effort="medium",
                                        task="optimize",
                                        prompt=prompts.OPTIMIZE_TASK.format(
                                            analysis=self._analysis_text(state), context=context, sql=sql,
                                            message=state["user_input"], feedback=feedback))
                candidate, detail = out.optimized_sql, f"{len(out.changes)} change(s) proposed"
            else:
                out = self.llm.generate(system=self._system(state), schema=ExplainResult, effort="low", task="explain",
                                        prompt=prompts.EXPLAIN_TASK.format(sql=sql, message=state["user_input"]))
                candidate, detail = sql, "Explanation written"
        except LLMError as e:
            return self._error(state, "sql_task", str(e))
        attempts = (state.get("attempts") or 0) + 1
        step = self._step(state, "sql_task", f"{intent}: {detail}")
        step["trace"][-1]["label"] = {"debug": "Debugging SQL", "optimize": "Optimizing SQL"}.get(intent, "Explaining SQL")
        return {"candidate_sql": clean_sql(candidate), "llm_output": out.model_dump(), "attempts": attempts,
                "producer": "sql_task", **step}

    @staticmethod
    def _analysis_text(state: AgentState) -> str:
        perf = state.get("user_performance") or {}
        uv = state.get("user_validation") or {}
        lines = [f"Estimated cost: {perf.get('cost_level')} (~{perf.get('estimated_rows_examined')} rows examined)"]
        lines += ["Query plan:"] + [f"  {p}" for p in perf.get("plan", [])]
        lines += [f"Index suggestion: {s['ddl']} ({s['reason']})" for s in perf.get("index_suggestions", [])]
        lines += [f"Note: {n}" for n in perf.get("notes", []) + uv.get("warnings", [])]
        lines += [f"Unused join: {u}" for u in perf.get("unused_joins", [])]
        return "\n".join(lines)

    @staticmethod
    def route_after_producer(state: AgentState) -> str:
        return "finalize" if state.get("response") else "validate_sql"

    # ---- 5. validation (with retry loop) -----------------------------------------------------
    def validate_sql(self, state: AgentState) -> dict[str, Any]:
        v = validate_sql(state.get("candidate_sql") or "", self.catalog, self.s.db_path)
        attempt = state.get("attempts") or 1
        if v.ok:
            detail = f"Passed (attempt {attempt})" + (f" with {len(v.warnings)} warning(s)" if v.warnings else "")
        else:
            retrying = attempt < self.s.max_sql_attempts
            detail = f"Attempt {attempt} failed: {v.errors[0]}" + (" (retrying)" if retrying else "")
        return {"validation": v.to_dict(), **self._step(state, "validate_sql", detail)}

    def route_after_validate(self, state: AgentState) -> str:
        if state["validation"]["ok"]:
            return "analyze_performance"
        if (state.get("attempts") or 0) < self.s.max_sql_attempts:
            return state.get("producer") or "fail"
        return "fail"

    def fail(self, state: AgentState) -> dict[str, Any]:
        uv = state.get("user_validation") or {}
        if state.get("intent") == "optimize" and uv.get("ok"):
            # The original query is valid; keep it rather than returning an unusable rewrite.
            out = dict(state.get("llm_output") or {})
            out.update(changes=["No valid rewrite was found, so the original query is kept."],
                       explanation=out.get("explanation", ""))
            return {"candidate_sql": state["user_sql"], "validation": uv, "llm_output": out,
                    **self._step(state, "fail", "Rewrite invalid; falling back to the original query")}
        v = state.get("validation") or {}
        out = state.get("llm_output") or {}
        message = (f"I couldn't produce a valid query after {state.get('attempts')} attempt(s). "
                   "Try rephrasing, or name the tables and columns you're interested in.")
        response = {"type": "error", "intent": state.get("intent"), "message": message,
                    "sql": v.get("sql"), "issues": out.get("issues", []),
                    "validation": {**{k: v.get(k) for k in ("ok", "errors", "warnings", "tables")},
                                   "attempts": state.get("attempts")}}
        return {"response": response, **self._step(state, "fail", "Giving up after maximum attempts")}

    @staticmethod
    def route_after_fail(state: AgentState) -> str:
        return "finalize" if state.get("response") else "analyze_performance"

    # ---- 6. optimization analysis ------------------------------------------------------------
    def analyze_performance(self, state: AgentState) -> dict[str, Any]:
        v = state["validation"]
        perf = analyze_performance(v["sql"], self.catalog, v.get("plan") or []).to_dict()
        # For explicit optimize requests, merge the model's index advice with the plan-based advice.
        def target(ddl: str) -> str:  # "Orders(Status)" part, whitespace/case/semicolon-insensitive
            return "".join(ddl.rstrip("; ").lower().split(" on ", 1)[-1].split())

        for rec in (state.get("llm_output") or {}).get("index_recommendations", []) or []:
            ddl = " ".join(rec["ddl"].split()).rstrip(";") + ";"
            if ddl.upper().startswith("CREATE INDEX") and all(
                    target(s["ddl"]) != target(ddl) for s in perf["index_suggestions"]):
                perf["index_suggestions"].append({"table": "", "columns": [], "ddl": ddl, "reason": rec["reason"]})
        detail = (f"Estimated cost {perf['cost_level']} (~{perf['estimated_rows_examined']} rows examined), "
                  f"{len(perf['index_suggestions'])} index suggestion(s)")
        return {"performance": perf, **self._step(state, "analyze_performance", detail)}

    @staticmethod
    def route_after_analyze(state: AgentState) -> str:
        return "execute_sql" if state.get("execute") else "generate_explanation"

    # ---- 7. execution (optional) ------------------------------------------------------------
    def _run(self, sql: str, max_rows: int) -> dict[str, Any]:
        try:
            return {**asdict(execute_readonly(self.s.db_path, sql, max_rows, self.s.query_timeout_s)), "error": None}
        except (sqlite3.Error, TimeoutError) as e:
            return {"columns": [], "rows": [], "row_count": 0, "truncated": False, "elapsed_ms": 0, "error": str(e)}

    def execute_sql(self, state: AgentState) -> dict[str, Any]:
        sql = state["validation"]["sql"]
        execution = self._run(sql, self.s.max_result_rows)
        update: dict[str, Any] = {"execution": execution}
        detail = (f"Error: {execution['error']}" if execution["error"] else
                  f"{execution['row_count']}{'+' if execution['truncated'] else ''} row(s) in {execution['elapsed_ms']} ms")

        original = state.get("user_sql")
        if state.get("intent") == "optimize" and original and " ".join(original.split()) != " ".join(sql.split()):
            eq = self._compare(original, sql)
            update["equivalence"] = eq
            detail += f"; equivalence check: {eq['status']}"
            if eq["status"] == "different":
                # Never hand back a "faster" query that changes the answer.
                out = dict(state.get("llm_output") or {})
                out["changes"] = [f"The rewrite returned different results on the sample data ({eq['detail']}), "
                                  "so the original query is kept."]
                update.update(validation=state["user_validation"], performance=state.get("user_performance"),
                              llm_output=out, execution=self._run(original, self.s.max_result_rows))
        return {**update, **self._step(state, "execute_sql", detail)}

    def _compare(self, original: str, rewritten: str) -> dict[str, str]:
        a, b = self._run(original, self.s.max_export_rows), self._run(rewritten, self.s.max_export_rows)
        if a["error"] or b["error"]:
            return {"status": "not_checked", "detail": a["error"] or b["error"]}
        if a["truncated"] or b["truncated"]:
            return {"status": "not_checked", "detail": "result too large to compare"}
        if len(a["columns"]) != len(b["columns"]):
            return {"status": "different", "detail": f"{len(a['columns'])} vs {len(b['columns'])} columns"}
        ordered = " order by " in f" {' '.join(original.lower().split())} "
        rows_a, rows_b = a["rows"], b["rows"]
        if not ordered:
            rows_a, rows_b = sorted(map(repr, rows_a)), sorted(map(repr, rows_b))
        if rows_a != rows_b:
            return {"status": "different", "detail": f"{len(a['rows'])} vs {len(b['rows'])} rows"}
        return {"status": "equivalent", "detail": f"same {len(a['rows'])} row(s)"}

    # ---- 8. explanation & response assembly --------------------------------------------------
    def generate_explanation(self, state: AgentState) -> dict[str, Any]:
        """The explanation text is written in the same LLM call as the SQL, and that call is
        redone on every retry, so it always describes the final query. This node adds the
        execution facts and assembles the payload."""
        intent = state["intent"]
        out = state.get("llm_output") or {}
        v = state["validation"]
        execution = state.get("execution")
        sql = v["sql"]
        explanation = out.get("explanation", "").strip()

        if intent == "debug":
            n = len(out.get("issues", []))
            message = f"I found {n} issue{'s' if n != 1 else ''} and corrected the query. {explanation}"
        elif intent == "optimize" and (state.get("equivalence") or {}).get("status") == "different":
            message = ("The rewrite I tried returned different results on the sample data, so I kept your query "
                       "unchanged. The details panel has index and performance suggestions.")
            explanation = ""
        elif intent == "optimize":
            message = "Here's the optimized query. " + explanation
        else:
            message = explanation or "Here's the query."
        if execution:
            if execution["error"]:
                message += f" Running it failed: {execution['error']}"
            else:
                more = " (showing the first rows)" if execution["truncated"] else ""
                message += f" It returned {execution['row_count']} row{'s' if execution['row_count'] != 1 else ''}{more}."

        response = {
            "type": "sql", "intent": intent, "message": message.strip(), "sql": sql,
            "original_sql": state.get("user_sql") if intent in ("optimize", "debug") else None,
            "explanation": explanation, "steps": out.get("steps", []), "assumptions": out.get("assumptions", []),
            "issues": out.get("issues", []), "changes": out.get("changes", []),
            "validation": {"ok": v["ok"], "errors": v["errors"], "warnings": v["warnings"], "tables": v["tables"],
                           "attempts": state.get("attempts")},
            "performance": state.get("performance"), "execution": execution, "equivalence": state.get("equivalence"),
            "dialects": {"sqlite": sql, "postgres": transpile(sql, "postgres"), "mysql": transpile(sql, "mysql")},
        }
        return {"response": response, **self._step(state, "generate_explanation")}

    # ---- terminal responses ------------------------------------------------------------------
    def refuse(self, state: AgentState) -> dict[str, Any]:
        reason, detail, intent = state.get("block_reason"), state.get("block_detail"), state.get("intent")
        if reason == "empty":
            message = "Please type a question about the database or paste a SQL query."
        elif reason == "too_long":
            message = f"That message is too long ({detail}). Please shorten it."
        elif reason == "injection":
            message = ("I can't change my instructions or share my configuration. I can help with SQL questions "
                       "about the provided database, for example \"Show all customers from California\".")
        elif intent == "destructive":
            op = f"{detail.split()[0].rstrip(',')} statements" if detail else "queries that modify data or the schema"
            message = (f"Sorry, I can only work with read-only queries, so I can't run or generate {op} "
                       "(DELETE, UPDATE, INSERT, DROP, ALTER and TRUNCATE are not allowed). I can write a SELECT "
                       "that previews the rows such a change would affect, if that helps.")
        else:
            message = OUT_OF_SCOPE_MESSAGE
        kind = reason or intent or "out_of_scope"
        return {"response": {"type": "refusal", "intent": kind, "message": message},
                **self._step(state, "refuse", kind)}

    def help(self, state: AgentState) -> dict[str, Any]:
        tables = ", ".join(self.catalog.table_names())
        message = (
            "Hi! I'm a SQL assistant for the company database (tables: " + tables + "). I can:\n"
            "- turn questions into SQL, for example \"Show all employees hired after January 2024\"\n"
            "- refine the last query with follow-ups, for example \"Only those from California\"\n"
            "- explain, optimize or debug SQL you paste in\n"
            "- describe the schema, for example \"What columns does Orders have?\"\n"
            "I only run read-only queries, and only answer questions about this database."
        )
        return {"response": {"type": "info", "intent": "help", "message": message}, **self._step(state, "help")}

    def schema_info(self, state: AgentState) -> dict[str, Any]:
        mentioned = self.catalog.relevant_tables(state["user_input"])
        detailed = len(mentioned) < len(self.catalog.tables)
        lines = []
        for name in mentioned if detailed else self.catalog.table_names():
            t = self.catalog.table(name)
            if detailed:
                lines.append(f"**{t.name}** ({t.row_count} rows): {t.description}")
                fks = {fk.column: fk for fk in t.foreign_keys}
                for c in t.columns:
                    ref = f" → {fks[c.name].ref_table}.{fks[c.name].ref_column}" if c.name in fks else ""
                    key = " (primary key)" if c.primary_key else ""
                    lines.append(f"- `{c.name}` {c.type}{key}{ref}" + (f": {c.description}" if c.description else ""))
                lines.append("")
            else:
                lines.append(f"- **{t.name}** ({t.row_count} rows): {', '.join(c.name for c in t.columns)}")
        header = "" if detailed else "The database has these tables:\n"
        return {"response": {"type": "info", "intent": "schema_info", "message": header + "\n".join(lines).strip(),
                             "tables": mentioned}, **self._step(state, "schema_info")}

    def clarify(self, state: AgentState) -> dict[str, Any]:
        return {"response": {"type": "clarification", "intent": "clarify",
                             "message": state.get("clarification_question")}, **self._step(state, "clarify")}

    # ---- memory -----------------------------------------------------------------------------
    def finalize(self, state: AgentState) -> dict[str, Any]:
        response = dict(state.get("response") or {"type": "error", "message": "No response was produced."})
        is_sql = response.get("type") == "sql" and (response.get("validation") or {}).get("ok")
        update: dict[str, Any] = {"history": [
            {"role": "user", "content": state.get("user_input", "")},
            {"role": "assistant", "content": response.get("message", ""), "sql": response.get("sql") if is_sql else None},
        ]}
        if is_sql:
            update["last_sql"] = response["sql"]
        update["response"] = {**response, "trace": state.get("trace") or []}
        return update
