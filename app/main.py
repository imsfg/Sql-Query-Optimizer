"""FastAPI app: chat API (JSON + Server-Sent Events streaming), schema, CSV export, and the
static frontend.

Run:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field

from app.agent.graph import SQLAgent
from app.agent.nodes import Deps
from app.config import ROOT_DIR, Settings, settings as default_settings
from app.database import SchemaCatalog, execute_readonly
from app.llm import StructuredLLM, make_llm
from app.sql_validator import validate_sql
from data.seed import build_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sql_assistant")


class ChatRequest(BaseModel):
    message: str = Field(max_length=20_000)
    thread_id: str | None = Field(None, max_length=100)
    execute: bool = True


class ExportRequest(BaseModel):
    sql: str = Field(max_length=20_000)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def create_app(settings: Settings = default_settings, llm: StructuredLLM | None = None, checkpointer=None) -> FastAPI:
    build_database(settings.db_path)  # no-op when the file exists
    catalog = SchemaCatalog.from_sqlite(settings.db_path)
    if checkpointer is None:
        settings.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
        checkpointer = SqliteSaver(sqlite3.connect(settings.checkpoint_db_path, check_same_thread=False))
    agent = SQLAgent(Deps(settings, catalog, llm or make_llm(settings)), checkpointer)

    app = FastAPI(title="SQL Assistant", version="1.0.0")
    app.state.agent = agent

    # Handlers are sync functions: FastAPI runs them in a worker thread, which suits the
    # synchronous graph and SQLite.
    @app.post("/api/chat")
    def chat(req: ChatRequest) -> dict[str, Any]:
        thread_id = req.thread_id or str(uuid.uuid4())
        try:
            response = agent.run(thread_id, req.message, req.execute)
        except Exception as e:  # noqa: BLE001
            log.exception("chat failed")
            raise HTTPException(500, "Something went wrong while processing the request.") from e
        return {"thread_id": thread_id, "response": response}

    @app.post("/api/chat/stream")
    def chat_stream(req: ChatRequest) -> StreamingResponse:
        thread_id = req.thread_id or str(uuid.uuid4())

        def events() -> Iterator[str]:
            yield _sse({"event": "thread", "thread_id": thread_id})
            try:
                for event in agent.stream(thread_id, req.message, req.execute):
                    yield _sse(event)
            except Exception:  # noqa: BLE001
                log.exception("stream failed")
                yield _sse({"event": "error", "message": "Something went wrong while processing the request."})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/threads/{thread_id}")
    def thread(thread_id: str) -> dict[str, Any]:
        return {"thread_id": thread_id, "history": agent.history(thread_id)}

    @app.get("/api/schema")
    def schema() -> dict[str, Any]:
        return catalog.to_dict()

    @app.post("/api/export")
    def export_csv(req: ExportRequest) -> Response:
        # SQL from the client is untrusted: validate it again before running it.
        v = validate_sql(req.sql, catalog, settings.db_path)
        if not v.ok:
            raise HTTPException(400, "; ".join(v.errors))
        try:
            result = execute_readonly(settings.db_path, v.sql, settings.max_export_rows, settings.query_timeout_s * 2)
        except (sqlite3.Error, TimeoutError) as e:
            raise HTTPException(400, str(e)) from e
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(result.columns)
        writer.writerows(result.rows)
        headers = {"Content-Disposition": 'attachment; filename="query_results.csv"'}
        if result.truncated:
            headers["X-Truncated"] = f"first {settings.max_export_rows} rows"
        return Response(buf.getvalue(), media_type="text/csv", headers=headers)

    @app.get("/api/graph", response_class=PlainTextResponse)
    def graph() -> str:
        return agent.mermaid()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "provider": settings.llm_provider, "model": settings.llm_model,
                "tables": catalog.table_names()}

    app.mount("/", StaticFiles(directory=ROOT_DIR / "frontend", html=True), name="frontend")
    return app


app = create_app()
