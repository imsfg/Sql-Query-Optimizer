"""Deterministic SQL validation. Nothing here trusts the LLM: every query shown to the user
or executed goes through `validate_sql` first.

Checks, in order:
  1. parses as SQL (sqlglot)
  2. exactly one statement
  3. read-only: a SELECT / set operation, with no write/DDL/admin node anywhere in the tree
     (this catches `WITH x AS (DELETE ... RETURNING *) SELECT ...` and `SELECT ... INTO`)
  4. every table exists in the schema (CTE names excluded)
  5. every column exists on the table/alias it refers to (scope-aware, with suggestions)
  6. join conditions follow declared foreign keys
  7. SQLite itself can prepare the statement (EXPLAIN QUERY PLAN), which catches dialect
     mistakes such as YEAR(), ambiguous columns, and anything sqlglot is lenient about
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.database import SchemaCatalog, explain_query_plan

_FORBIDDEN_NODE_NAMES = {
    "Insert": "INSERT", "Update": "UPDATE", "Delete": "DELETE", "Drop": "DROP", "Alter": "ALTER",
    "TruncateTable": "TRUNCATE", "Create": "CREATE", "Merge": "MERGE", "Pragma": "PRAGMA",
    "Attach": "ATTACH", "Detach": "DETACH", "Transaction": "BEGIN", "Commit": "COMMIT",
    "Rollback": "ROLLBACK", "Use": "USE", "Set": "SET", "Copy": "COPY", "LoadData": "LOAD DATA",
    "Into": "SELECT INTO", "Command": None,  # Command: anything sqlglot could not parse structurally
}
_FORBIDDEN_NODES = {getattr(exp, n): kw for n, kw in _FORBIDDEN_NODE_NAMES.items() if hasattr(exp, n)}
_READ_ROOTS = (exp.Select, exp.SetOperation)
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit"}
_IMPLICIT_COLUMNS = {"rowid", "oid", "_rowid_"}


@dataclass
class ValidationResult:
    ok: bool
    sql: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    read_only: bool = True
    plan: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean_sql(sql: str) -> str:
    """Strip markdown fences, surrounding whitespace, and trailing semicolons."""
    s = sql.strip()
    fence = re.match(r"^```(?:sql)?\s*(.*?)\s*```$", s, re.S | re.I)
    if fence:
        s = fence.group(1)
    return s.strip().rstrip(";").strip()


def validate_sql(sql: str, catalog: SchemaCatalog, db_path: Path | None = None,
                 dialect: str = "sqlite") -> ValidationResult:
    cleaned = clean_sql(sql or "")
    result = ValidationResult(ok=False, sql=cleaned)
    if not cleaned:
        result.errors.append("The query is empty.")
        return result

    # 1. syntax
    try:
        statements = [s for s in sqlglot.parse(cleaned, read=dialect) if s is not None]
    except ParseError as e:
        result.errors.append(_syntax_error_message(e, cleaned, db_path))
        return result

    # 2. single statement
    if len(statements) != 1:
        result.errors.append(f"Exactly one SQL statement is allowed; found {len(statements)}.")
    if not statements:
        return result

    # 3. read-only
    for stmt in statements:
        violations = _write_operations(stmt)
        if violations or not isinstance(stmt, _READ_ROOTS):
            result.read_only = False
            ops = ", ".join(sorted(violations)) or type(stmt).__name__.upper()
            result.errors.append(f"Only read-only SELECT queries are allowed; found {ops}.")
    if not result.read_only or len(statements) != 1:
        return result

    tree = statements[0]
    # 4-5. tables and columns
    result.tables = _check_tables(tree, catalog, result.errors)
    if not result.errors:
        _check_columns(tree, catalog, result.errors)
    # 6. relationships
    if not result.errors:
        _check_joins(tree, catalog, result.errors, result.warnings)

    if any(_is_star(p) for sel in tree.find_all(exp.Select) for p in sel.expressions
           if not sel.find_ancestor(exp.Exists)):
        result.warnings.append("SELECT * returns every column; listing only the needed columns is cheaper and clearer.")

    # 7. engine check
    if not result.errors and db_path is not None:
        try:
            result.plan = explain_query_plan(db_path, cleaned)
        except sqlite3.Error as e:
            result.errors.append(f"SQLite rejected the query: {e}")

    result.ok = not result.errors
    return result


def _syntax_error_message(error: ParseError, sql: str, db_path: Path | None) -> str:
    # SQLite's own message ("near "SELEC": syntax error") is usually clearer than sqlglot's.
    if db_path is not None:
        try:
            explain_query_plan(db_path, sql)
        except sqlite3.Error as e:
            return f"Syntax error: {e}"
    first = error.errors[0] if error.errors else {}
    where = f" (line {first.get('line')}, col {first.get('col')})" if first.get("line") else ""
    return f"Syntax error{where}: {first.get('description') or str(error).splitlines()[0]}"


def _is_star(node: exp.Expression) -> bool:
    return isinstance(node, exp.Star) or (isinstance(node, exp.Column) and isinstance(node.this, exp.Star))


def _write_operations(tree: exp.Expression) -> set[str]:
    found = set()
    for node in tree.walk():
        for cls, keyword in _FORBIDDEN_NODES.items():
            if isinstance(node, cls):
                found.add(keyword or str(node.this).split()[0].upper())
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _DENIED_FUNCTIONS:
                found.add(f"{name.upper()}()")
    return found


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _suggest(name: str, options: list[str]) -> str:
    lower = {o.lower(): o for o in options}
    match = difflib.get_close_matches(name.lower(), list(lower), n=1, cutoff=0.6)
    return f" Did you mean '{lower[match[0]]}'?" if match else ""


def _check_tables(tree: exp.Expression, catalog: SchemaCatalog, errors: list[str]) -> list[str]:
    ctes = _cte_names(tree)
    used = []
    for table in tree.find_all(exp.Table):
        name = table.name
        if not name or name.lower() in ctes:
            continue
        real = catalog.table(name)
        if real is None:
            errors.append(f"Unknown table '{name}'.{_suggest(name, catalog.table_names())} "
                          f"Available tables: {', '.join(catalog.table_names())}.")
        elif real.name not in used:
            used.append(real.name)
    return used


def _resolve_source(scope: Scope | None, alias: str):
    """Find the source (exp.Table or derived Scope) for an alias, walking out to parent
    scopes so correlated subqueries resolve outer aliases."""
    while scope is not None:
        for key, src in scope.sources.items():
            if key.lower() == alias.lower():
                return src
        scope = scope.parent
    return None


def _derived_columns(src: Scope) -> set[str] | None:
    """Output columns of a derived table / CTE, or None when unknown (e.g. SELECT *)."""
    expr = src.expression
    if isinstance(expr, exp.Select) and not any(isinstance(s, exp.Star) for s in expr.expressions):
        return {n.lower() for n in expr.named_selects}
    return None


def _check_columns(tree: exp.Expression, catalog: SchemaCatalog, errors: list[str]) -> None:
    seen = set()

    def err(msg: str) -> None:
        if msg not in seen:
            seen.add(msg)
            errors.append(msg)

    for scope in traverse_scope(tree):
        select_aliases = set()
        if isinstance(scope.expression, exp.Select):
            select_aliases = {s.alias.lower() for s in scope.expression.expressions if s.alias}
        for col in scope.columns:
            # sqlglot sometimes lists a subquery's columns on the outer scope too (e.g. under
            # NOT IN); check each column only in the scope of its own SELECT.
            if isinstance(scope.expression, exp.Select) and col.find_ancestor(exp.Select) is not scope.expression:
                continue
            name = col.name
            if not name or name == "*" or isinstance(col.this, exp.Star) or name.lower() in _IMPLICIT_COLUMNS:
                continue
            qualifier = col.table
            if qualifier:
                src = _resolve_source(scope, qualifier)
                if src is None:
                    err(f"Unknown table or alias '{qualifier}' in '{qualifier}.{name}'.")
                elif isinstance(src, exp.Table):
                    table = catalog.table(src.name)
                    if table and not table.column(name):
                        err(f"Unknown column '{name}' on table {table.name}."
                            f"{_suggest(name, [c.name for c in table.columns])} "
                            f"Columns: {', '.join(c.name for c in table.columns)}.")
                else:
                    cols = _derived_columns(src)
                    if cols is not None and name.lower() not in cols:
                        err(f"Column '{name}' is not produced by subquery/CTE '{qualifier}'.")
                continue

            # Unqualified: must exist on some source visible from this scope, or be a SELECT alias.
            if name.lower() in select_aliases:
                continue
            candidates: list[str] = []
            found = False
            s: Scope | None = scope
            while s is not None and not found:
                for src in s.sources.values():
                    if isinstance(src, exp.Table):
                        table = catalog.table(src.name)
                        if table is None:
                            continue
                        candidates += [c.name for c in table.columns]
                        if table.column(name):
                            found = True
                    else:
                        cols = _derived_columns(src)
                        if cols is None or name.lower() in cols:
                            found = True
                s = s.parent
            if not found:
                err(f"Unknown column '{name}'.{_suggest(name, candidates)}")


def _alias_map(tree: exp.Expression, catalog: SchemaCatalog) -> dict[str, str]:
    ctes = _cte_names(tree)
    out = {}
    for table in tree.find_all(exp.Table):
        real = catalog.table(table.name) if table.name.lower() not in ctes else None
        if real:
            out[table.alias_or_name.lower()] = real.name
    return out


def _check_joins(tree: exp.Expression, catalog: SchemaCatalog, errors: list[str], warnings: list[str]) -> None:
    aliases = _alias_map(tree, catalog)
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            if not join.args.get("using") and join.args.get("kind", "").upper() != "CROSS":
                target = join.this.alias_or_name if isinstance(join.this, exp.Table) else "a subquery"
                warnings.append(f"Join with {target} has no ON condition, which produces a cartesian product "
                                "unless it is filtered in WHERE.")
            continue
        for eq in on.find_all(exp.EQ):
            left, right = eq.left, eq.right
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column) and left.table and right.table):
                continue
            lt, rt = aliases.get(left.table.lower()), aliases.get(right.table.lower())
            if not lt or not rt:
                continue
            if catalog.has_fk(lt, left.name, rt, right.name):
                continue
            text = f"{lt}.{left.name} = {rt}.{right.name}"
            valid = _relationships(catalog, lt, rt)
            hint = f" Declared relationships: {'; '.join(valid)}." if valid else \
                f" No foreign key links {lt} and {rt} directly."
            if catalog.is_key_column(lt, left.name) and catalog.is_key_column(rt, right.name):
                errors.append(f"Join condition {text} does not match a declared relationship.{hint}")
            else:
                warnings.append(f"Join condition {text} is not a declared foreign key.{hint}")


def _relationships(catalog: SchemaCatalog, a: str, b: str) -> list[str]:
    out = []
    for src, dst in ((a, b), (b, a)):
        t = catalog.table(src)
        for fk in t.foreign_keys if t else []:
            if fk.ref_table.lower() == dst.lower():
                out.append(f"{t.name}.{fk.column} -> {fk.ref_table}.{fk.ref_column}")
    return out


def transpile(sql: str, target: str, source: str = "sqlite") -> str | None:
    """Render a validated query in another dialect (display only, never executed)."""
    try:
        return sqlglot.transpile(sql, read=source, write=target, pretty=True)[0]
    except Exception:  # noqa: BLE001 - best effort; a missing translation is not an error
        return None
