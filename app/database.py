"""Database access: schema introspection and sandboxed, read-only query execution."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DOCS_PATH = Path(__file__).resolve().parent.parent / "data" / "schema_docs.json"

# Low-cardinality text columns get their distinct values listed in the LLM prompt,
# which is what lets "customers from California" become State = 'CA'.
MAX_SAMPLE_VALUES = 12
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass
class Column:
    name: str
    type: str
    not_null: bool
    primary_key: bool
    description: str = ""
    sample_values: list[str] = field(default_factory=list)
    value_range: tuple[str, str] | None = None


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class Table:
    name: str
    columns: list[Column]
    foreign_keys: list[ForeignKey]
    indexes: dict[str, list[str]]
    row_count: int
    description: str = ""

    def column(self, name: str) -> Column | None:
        lname = name.lower()
        return next((c for c in self.columns if c.name.lower() == lname), None)


class SchemaCatalog:
    """In-memory description of the database schema, the single source of truth for
    prompt construction, SQL validation, and relationship checks."""

    def __init__(self, tables: list[Table]):
        self.tables: dict[str, Table] = {t.name: t for t in tables}
        self._by_lower = {t.name.lower(): t for t in tables}

    # ---- lookups -------------------------------------------------------------------
    def table(self, name: str) -> Table | None:
        return self._by_lower.get(name.lower())

    def table_names(self) -> list[str]:
        return list(self.tables)

    def has_fk(self, table_a: str, col_a: str, table_b: str, col_b: str) -> bool:
        """True when a declared foreign key links table_a.col_a and table_b.col_b (either direction)."""
        for (src, sc, dst, dc) in ((table_a, col_a, table_b, col_b), (table_b, col_b, table_a, col_a)):
            t = self.table(src)
            if t and any(
                fk.column.lower() == sc.lower() and fk.ref_table.lower() == dst.lower() and fk.ref_column.lower() == dc.lower()
                for fk in t.foreign_keys
            ):
                return True
        return False

    def is_key_column(self, table: str, column: str) -> bool:
        t = self.table(table)
        if not t:
            return False
        col = t.column(column)
        return bool(col and (col.primary_key or any(fk.column.lower() == column.lower() for fk in t.foreign_keys)))

    def indexed_leading_columns(self, table: str) -> set[str]:
        t = self.table(table)
        if not t:
            return set()
        leading = {cols[0].lower() for cols in t.indexes.values() if cols}
        leading.update(c.name.lower() for c in t.columns if c.primary_key)
        return leading

    def sqlglot_schema(self) -> dict[str, dict[str, str]]:
        return {t.name: {c.name: c.type or "TEXT" for c in t.columns} for t in self.tables.values()}

    # ---- schema retrieval ------------------------------------------------------------
    def relevant_tables(self, question: str, extra_sql: str | None = None) -> list[str]:
        """Lexical retrieval: tables whose name/columns are mentioned, plus FK neighbours.

        Returns every table when nothing matches, so retrieval can only narrow context,
        never hide the table the user meant."""
        text = f"{question} {extra_sql or ''}".lower()
        words = set(re.findall(r"[a-z]+", text))
        stems = {w.rstrip("s") for w in words} | words

        def tokens(name: str) -> set[str]:
            parts = re.findall(r"[A-Z][a-z]*|[a-z]+", name)
            out = {p.lower() for p in parts} | {name.lower()}
            return out | {p.rstrip("s") for p in out}

        hits = []
        for t in self.tables.values():
            if tokens(t.name) & stems or any(c.name.lower() in words for c in t.columns if not c.primary_key):
                hits.append(t.name)
        if not hits:
            return self.table_names()
        selected = set(hits)
        for name in hits:
            for fk in self.tables[name].foreign_keys:
                selected.add(self.table(fk.ref_table).name if self.table(fk.ref_table) else fk.ref_table)
        hit_names = {h.lower() for h in hits}
        for t in self.tables.values():  # bridge tables that link two of the hits
            if len({fk.ref_table.lower() for fk in t.foreign_keys} & hit_names) >= 2:
                selected.add(t.name)
        return [n for n in self.tables if n in selected]

    # ---- rendering -----------------------------------------------------------------
    def prompt_text(self, tables: list[str] | None = None) -> str:
        """Render the schema as annotated DDL for the LLM."""
        names = tables or self.table_names()
        blocks = []
        for name in names:
            t = self.table(name)
            if not t:
                continue
            header = f"CREATE TABLE {t.name} (  -- {t.description} (~{t.row_count} rows)".rstrip()
            lines = []
            fk_by_col = {fk.column.lower(): fk for fk in t.foreign_keys}
            for c in t.columns:
                parts = [f"  {c.name} {c.type}".rstrip()]
                if c.primary_key:
                    parts.append("PRIMARY KEY")
                elif c.not_null:
                    parts.append("NOT NULL")
                fk = fk_by_col.get(c.name.lower())
                if fk:
                    parts.append(f"REFERENCES {fk.ref_table}({fk.ref_column})")
                notes = []
                if c.description:
                    notes.append(c.description)
                if c.sample_values:
                    notes.append("values: " + ", ".join(f"'{v}'" for v in c.sample_values))
                if c.value_range:
                    notes.append(f"range: {c.value_range[0]} to {c.value_range[1]}")
                line = " ".join(parts) + ","
                if notes:
                    line += "  -- " + "; ".join(notes)
                lines.append(line)
            block = header + "\n" + "\n".join(lines) + "\n);"
            if t.indexes:
                block += "\n-- indexes: " + "; ".join(f"{n}({', '.join(cols)})" for n, cols in t.indexes.items())
            blocks.append(block)
        return "\n\n".join(blocks)

    def to_dict(self) -> dict[str, Any]:
        return {"tables": [asdict(t) for t in self.tables.values()]}

    # ---- construction --------------------------------------------------------------
    @classmethod
    def from_sqlite(cls, db_path: Path, docs_path: Path = DOCS_PATH) -> "SchemaCatalog":
        docs = json.loads(docs_path.read_text()) if docs_path.exists() else {}
        conn = connect_readonly(db_path, authorize=False)
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            )]
            tables = []
            for name in names:
                tdoc = docs.get(name, {})
                cdocs = tdoc.get("columns", {})
                row_count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                columns = []
                for _, cname, ctype, notnull, _, pk in conn.execute(f'PRAGMA table_info("{name}")'):
                    col = Column(cname, ctype.upper(), bool(notnull), bool(pk), cdocs.get(cname, ""))
                    if not pk and ctype.upper() == "TEXT":
                        _profile_text_column(conn, name, col)
                    columns.append(col)
                fks = [ForeignKey(r[3], r[2], r[4]) for r in conn.execute(f'PRAGMA foreign_key_list("{name}")')]
                indexes = {}
                for idx in conn.execute(f'PRAGMA index_list("{name}")'):
                    idx_name, origin = idx[1], idx[3]
                    if origin == "c":  # explicit CREATE INDEX (skip autoindexes for UNIQUE/PK)
                        indexes[idx_name] = [r[2] for r in conn.execute(f'PRAGMA index_info("{idx_name}")')]
                tables.append(Table(name, columns, fks, indexes, row_count, tdoc.get("description", "")))
        finally:
            conn.close()
        return cls(tables)


def _profile_text_column(conn: sqlite3.Connection, table: str, col: Column) -> None:
    distinct = conn.execute(f'SELECT COUNT(DISTINCT "{col.name}") FROM "{table}"').fetchone()[0]
    if 0 < distinct <= MAX_SAMPLE_VALUES and "email" not in col.name.lower():
        col.sample_values = [r[0] for r in conn.execute(
            f'SELECT DISTINCT "{col.name}" FROM "{table}" WHERE "{col.name}" IS NOT NULL ORDER BY 1'
        )]
        return
    lo, hi = conn.execute(f'SELECT MIN("{col.name}"), MAX("{col.name}") FROM "{table}"').fetchone()
    if isinstance(lo, str) and _DATE_RE.match(lo) and isinstance(hi, str) and _DATE_RE.match(hi):
        col.value_range = (lo, hi)


# ---- sandboxed execution ----------------------------------------------------------------

# Authorizer allow-list: reading tables and calling (non-dangerous) functions only.
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _src: str | None) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def connect_readonly(db_path: Path, authorize: bool = True) -> sqlite3.Connection:
    """Open the database so that writes are impossible at three levels: the file is opened
    read-only, the connection is put in query_only mode, and an authorizer rejects every
    action except reads."""
    uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    if authorize:
        conn.set_authorizer(_authorizer)
    return conn


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float


def _json_safe(value: Any) -> Any:
    return value.hex() if isinstance(value, (bytes, bytearray)) else value


def execute_readonly(db_path: Path, sql: str, max_rows: int, timeout_s: float) -> QueryResult:
    conn = connect_readonly(db_path)
    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    start = time.perf_counter()
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or []]
        fetched = cur.fetchmany(max_rows + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise TimeoutError(f"Query exceeded the {timeout_s:g}s execution limit") from e
        raise
    finally:
        conn.close()
    truncated = len(fetched) > max_rows
    rows = [[_json_safe(v) for v in r] for r in fetched[:max_rows]]
    return QueryResult(columns, rows, len(rows), truncated, round((time.perf_counter() - start) * 1000, 2))


def explain_query_plan(db_path: Path, sql: str) -> list[dict[str, Any]]:
    """EXPLAIN QUERY PLAN. Also serves as the engine-level syntax/semantic check: SQLite
    prepares the statement, so unknown functions or columns surface here as errors."""
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "parent": r[1], "detail": r[3]} for r in rows]
