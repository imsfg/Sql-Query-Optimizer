"""Deterministic performance analysis of a validated query.

Uses SQLite's EXPLAIN QUERY PLAN plus the query AST to produce:
  * a heuristic cost estimate (rows examined, low/medium/high)
  * index recommendations for full scans on filtered / joined columns
  * lint notes (non-sargable predicates, leading-wildcard LIKE, NOT IN, correlated
    subqueries, redundant DISTINCT)
  * joins whose table contributes no columns (candidates for removal)

These findings are shown to the user directly and handed to the LLM as hard evidence
when it is asked to optimize a query.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from app.database import SchemaCatalog

_LOOP_RE = re.compile(r"^(SCAN|SEARCH) (?:TABLE )?([\w]+)(?: AS (\w+))?(.*)$")
_COMPARISONS = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_UNKNOWN_SOURCE_ROWS = 100


@dataclass
class IndexSuggestion:
    table: str
    columns: list[str]
    ddl: str
    reason: str


@dataclass
class PerformanceReport:
    cost_level: str = "unknown"
    estimated_rows_examined: int = 0
    plan: list[str] = field(default_factory=list)
    index_suggestions: list[IndexSuggestion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unused_joins: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze_performance(sql: str, catalog: SchemaCatalog, plan: list[dict[str, Any]]) -> PerformanceReport:
    report = PerformanceReport()
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001 - analysis is advisory; validation already reported syntax
        return report

    aliases = _alias_map(tree, catalog)
    report.plan = _render_plan(plan)
    loops = _plan_loops(plan, aliases, catalog)
    report.estimated_rows_examined, report.cost_level = _estimate_cost(loops)
    if any("USE TEMP B-TREE" in p["detail"] for p in plan):
        report.notes.append("Sorting/grouping needs a temporary B-tree; an index matching the ORDER BY/GROUP BY "
                            "columns can avoid it for large tables.")

    where_cols, join_cols = _predicate_columns(tree, aliases, catalog)
    report.index_suggestions = _suggest_indexes(loops, where_cols, join_cols, catalog)
    report.notes += _lint(tree)
    report.unused_joins = _unused_joins(tree, aliases, catalog)
    return report


# ---- plan & cost ------------------------------------------------------------------

def _alias_map(tree: exp.Expression, catalog: SchemaCatalog) -> dict[str, str]:
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out = {}
    for t in tree.find_all(exp.Table):
        real = catalog.table(t.name) if t.name.lower() not in ctes else None
        if real:
            out[t.alias_or_name.lower()] = real.name
            out.setdefault(real.name.lower(), real.name)
    return out


def _render_plan(plan: list[dict[str, Any]]) -> list[str]:
    depth = {0: -1}
    lines = []
    for row in plan:
        d = depth.get(row["parent"], -1) + 1
        depth[row["id"]] = d
        lines.append("  " * d + row["detail"])
    return lines


def _plan_loops(plan: list[dict[str, Any]], aliases: dict[str, str], catalog: SchemaCatalog) -> list[dict]:
    by_id = {row["id"]: row for row in plan}
    loops = []
    for position, row in enumerate(plan):
        m = _LOOP_RE.match(row["detail"])
        if not m:
            continue
        kind, name, alias, rest = m.groups()
        real = aliases.get((alias or name).lower()) or aliases.get(name.lower())
        table = catalog.table(real) if real else None
        # A loop is part of the outer join nest unless it sits inside a non-correlated subquery.
        correlated, nested, parent = False, False, row["parent"]
        while parent in by_id:
            detail = by_id[parent]["detail"]
            nested = True
            correlated = correlated or "CORRELATED" in detail
            parent = by_id[parent]["parent"]
        loops.append({
            "kind": kind, "name": alias or name, "table": table.name if table else None,
            "rows": table.row_count if table else _UNKNOWN_SOURCE_ROWS, "detail": rest.strip(),
            "multiplies": not nested or correlated, "position": position,
        })
    return loops


def _estimate_cost(loops: list[dict]) -> tuple[int, str]:
    if not loops:
        return 0, "low"
    product, additive = 1.0, 0.0
    for loop in loops:
        n = max(loop["rows"], 1)
        if loop["kind"] == "SCAN":
            factor = float(n)
        elif "PRIMARY KEY" in loop["detail"]:
            factor = 1.0
        else:
            factor = max(1.0, math.log2(n + 1))
        if loop["multiplies"]:
            product *= factor
        else:
            additive += factor
    work = int(product + additive)
    level = "low" if work < 10_000 else "medium" if work < 1_000_000 else "high"
    return work, level


# ---- index suggestions ------------------------------------------------------------------

def _column_table(col: exp.Column, aliases: dict[str, str], catalog: SchemaCatalog) -> str | None:
    if col.table:
        return aliases.get(col.table.lower())
    owners = {t for t in set(aliases.values()) if catalog.table(t) and catalog.table(t).column(col.name)}
    return owners.pop() if len(owners) == 1 else None


def _predicate_columns(tree: exp.Expression, aliases: dict[str, str], catalog: SchemaCatalog):
    """(table -> [(column, is_equality)]) for WHERE filters and for JOIN keys."""
    where_cols: dict[str, list[tuple[str, bool]]] = {}
    join_cols: dict[str, list[tuple[str, bool]]] = {}

    def add(target: dict, col: exp.Column, equality: bool) -> None:
        table = _column_table(col, aliases, catalog)
        real_col = catalog.table(table).column(col.name) if table else None
        if real_col and (real_col.name, equality) not in target.setdefault(table, []):
            target[table].append((real_col.name, equality))

    def collect(root: exp.Expression | None, target: dict) -> None:
        if root is None:
            return
        for pred in root.find_all(*_COMPARISONS, exp.Between, exp.In, exp.Like):
            eq = isinstance(pred, (exp.EQ, exp.In))
            if isinstance(pred, _COMPARISONS):
                for side in (pred.left, pred.right):
                    if isinstance(side, exp.Column):
                        add(target, side, eq)
            elif isinstance(pred.this, exp.Column):
                if isinstance(pred, exp.Like):
                    pattern = pred.expression
                    if isinstance(pattern, exp.Literal) and pattern.this.startswith("%"):
                        continue
                add(target, pred.this, eq)

    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        collect(where.this if where else None, where_cols)
        for join in select.args.get("joins") or []:
            collect(join.args.get("on"), join_cols)
    return where_cols, join_cols


def _suggest_indexes(loops, where_cols, join_cols, catalog: SchemaCatalog) -> list[IndexSuggestion]:
    suggestions: list[IndexSuggestion] = []
    first_outer = next((l["position"] for l in loops if l["multiplies"]), None)
    for loop in loops:
        table = loop["table"]
        if loop["kind"] != "SCAN" or not table:
            continue
        indexed = catalog.indexed_leading_columns(table)
        cols = list(where_cols.get(table, []))
        reason = "filtered on"
        # An inner loop that scans the whole table for every outer row needs an index on the join key.
        if loop["position"] != first_outer:
            cols += join_cols.get(table, [])
            reason = "filtered/joined on"
        cols = [c for c in cols if c[0].lower() not in indexed]
        if not cols:
            continue
        ordered = [c for c, eq in cols if eq] + [c for c, eq in cols if not eq][:1]  # equality first, one range
        ordered = list(dict.fromkeys(ordered))[:3]
        name = f"idx_{table.lower()}_{'_'.join(c.lower() for c in ordered)}"
        ddl = f"CREATE INDEX {name} ON {table}({', '.join(ordered)});"
        if all(s.ddl != ddl for s in suggestions):
            suggestions.append(IndexSuggestion(
                table, ordered, ddl,
                f"Full scan of {table} (~{loop['rows']} rows) {reason} {', '.join(ordered)}.",
            ))
    return suggestions


# ---- lint ---------------------------------------------------------------------------

def _lint(tree: exp.Expression) -> list[str]:
    notes = []
    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        if where is not None:
            for pred in where.find_all(*_COMPARISONS, exp.Like, exp.In, exp.Between):
                target = pred.this if not isinstance(pred, _COMPARISONS) else pred.left
                if isinstance(target, exp.Func) and target.find(exp.Column) is not None:
                    col = target.find(exp.Column).name
                    notes.append(f"Filtering on a function of {col} ({target.sql('sqlite')}) prevents index use; "
                                 f"compare the raw column instead (e.g. a date range on {col}).")
                if isinstance(pred, exp.Like) and isinstance(pred.expression, exp.Literal) \
                        and pred.expression.this.startswith("%"):
                    notes.append(f"LIKE '{pred.expression.this}' starts with a wildcard, so it cannot use an index.")
            for not_node in where.find_all(exp.Not):
                if isinstance(not_node.this, exp.In) and not_node.this.args.get("query"):
                    notes.append("NOT IN (subquery) returns no rows if the subquery yields a NULL and is often slower; "
                                 "prefer NOT EXISTS.")
        if select.args.get("distinct") and select.args.get("group"):
            notes.append("DISTINCT is redundant together with GROUP BY on the same columns.")
        for proj in select.expressions:
            sub = proj.find(exp.Subquery)
            if sub is not None and _is_correlated(sub, select):
                notes.append("A correlated subquery in the SELECT list runs once per row; a JOIN with GROUP BY "
                             "is usually faster.")
                break
    return list(dict.fromkeys(notes))


def _is_correlated(sub: exp.Subquery, outer: exp.Select) -> bool:
    inner_aliases = {t.alias_or_name.lower() for t in sub.find_all(exp.Table)}
    return any(c.table and c.table.lower() not in inner_aliases for c in sub.find_all(exp.Column))


def _unused_joins(tree: exp.Expression, aliases: dict[str, str], catalog: SchemaCatalog) -> list[str]:
    """Joined tables none of whose columns are referenced outside their own ON clause."""
    findings = []
    for select in tree.find_all(exp.Select):
        if any(isinstance(p, exp.Star) for p in select.expressions):
            continue
        for join in select.args.get("joins") or []:
            src = join.this
            if not isinstance(src, exp.Table) or not catalog.table(src.name):
                continue
            alias = src.alias_or_name.lower()
            table = catalog.table(src.name)
            on = join.args.get("on")
            used = False
            for col in select.find_all(exp.Column):
                if on is not None and col.find_ancestor(exp.Join) is join:
                    continue
                if isinstance(col.this, exp.Star) and col.table.lower() == alias:
                    used = True
                elif col.table:
                    used = used or col.table.lower() == alias
                else:
                    used = used or (_column_table(col, aliases, catalog) == table.name)
                if used:
                    break
            if used:
                continue
            side = (join.args.get("side") or "").upper()
            on_pk = on is not None and any(
                isinstance(c, exp.Column) and c.table.lower() == alias and table.column(c.name)
                and table.column(c.name).primary_key for c in on.find_all(exp.Column)
            )
            if side == "LEFT" and on_pk:
                findings.append(f"LEFT JOIN {table.name} ({src.alias_or_name}) contributes no columns and matches on "
                                "its primary key, so it can be removed without changing the result.")
            else:
                findings.append(f"JOIN {table.name} ({src.alias_or_name}) contributes no columns; it only matters if "
                                "it is meant to filter rows (e.g. keep only rows with a match).")
    return findings
