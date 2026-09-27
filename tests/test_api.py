import json

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.main import create_app
from tests.fakes import FakeLLM, generated, intent


@pytest.fixture
def client_and_llm(settings):
    llm = FakeLLM({"classify": [intent("generate"), intent("modify")],
                   "generate": [generated("SELECT FirstName, State FROM Customers"),
                                generated("SELECT FirstName, State FROM Customers WHERE State = 'CA'")]})
    return TestClient(create_app(settings, llm=llm, checkpointer=InMemorySaver())), llm


def test_chat_and_thread_history(client_and_llm):
    client, _ = client_and_llm
    first = client.post("/api/chat", json={"message": "Show all customers"}).json()
    thread_id = first["thread_id"]
    assert first["response"]["type"] == "sql"
    second = client.post("/api/chat", json={"message": "Only those from California", "thread_id": thread_id}).json()
    assert second["response"]["sql"].endswith("WHERE State = 'CA'")
    history = client.get(f"/api/threads/{thread_id}").json()["history"]
    assert len(history) == 4 and history[-1]["sql"].endswith("'CA'")


def test_chat_stream_sse(client_and_llm):
    client, _ = client_and_llm
    with client.stream("POST", "/api/chat/stream", json={"message": "Show all customers"}) as res:
        assert res.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in res.iter_lines() if line.startswith("data: ")]
    assert events[0]["event"] == "thread"
    assert any(e["event"] == "step" and e["node"] == "validate_sql" for e in events)
    assert events[-1]["event"] == "result" and events[-1]["response"]["type"] == "sql"


def test_export_csv_revalidates_sql(client_and_llm):
    client, _ = client_and_llm
    ok = client.post("/api/export", json={"sql": "SELECT Name, UnitPrice FROM Products ORDER BY Name LIMIT 2"})
    assert ok.status_code == 200 and ok.text.splitlines()[0] == "Name,UnitPrice"
    bad = client.post("/api/export", json={"sql": "DELETE FROM Orders"})
    assert bad.status_code == 400 and "read-only" in bad.json()["detail"]


def test_schema_health_and_frontend(client_and_llm):
    client, _ = client_and_llm
    tables = client.get("/api/schema").json()["tables"]
    assert {t["name"] for t in tables} >= {"Customers", "Orders"}
    assert client.get("/api/health").json()["status"] == "ok"
    assert "validate_sql" in client.get("/api/graph").text
    assert "SQL Assistant" in client.get("/").text
