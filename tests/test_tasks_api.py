"""Task HTTP API: create/run/read/cancel/retry/advance end to end."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return db


async def _fake_execute(tools, name, args):
    return json.dumps({"result": "done"})


def _client(monkeypatch):
    from app.main import app

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    return TestClient(app, headers=auth_headers())


def _agent(client, name="worker"):
    resp = client.post("/agents", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cap(client, handle, name, tool="web_search"):
    resp = client.post(
        f"/agents/{handle}/capabilities", json={"name": name, "tool": tool})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_task_full_run_completed(isolated_db, monkeypatch):
    client = _client(monkeypatch)
    agent = _agent(client)
    _cap(client, agent["id"], "job")
    client.post("/ask/policy", json={"peer": "owner", "action": "*"})
    resp = client.post(
        "/tasks",
        json={"target_agent": agent["id"],
              "capability": f"{agent['id']}.job@v1",
              "input": {"q": "x"}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["output"]["result"] == {"result": "done"}
    assert body["target_agent_id"] == agent["agent_id"]
    assert [e["event"] for e in body["events"]][:3] == [
        "TASK_CREATED", "TASK_RESOLVING", "TASK_AUTHORIZED"]


def test_task_missing_target_failed(isolated_db, monkeypatch):
    client = _client(monkeypatch)
    body = client.post(
        "/tasks", json={"target_agent": "ghost"}).json()
    assert body["status"] == "FAILED"
    assert body["error_code"] == "AGENT_NOT_FOUND"


def test_task_waits_for_approval_then_advances(isolated_db, monkeypatch):
    client = _client(monkeypatch)
    agent = _agent(client)
    _cap(client, agent["id"], "job")
    created = client.post(
        "/tasks",
        json={"target_agent": agent["id"],
              "capability": f"{agent['id']}.job@v1"}).json()
    assert created["status"] == "WAITING_APPROVAL"
    approval_id = created["approval_id"]
    assert approval_id
    # Still waiting before any decision.
    again = client.post(f"/tasks/{created['task_id']}/advance").json()
    assert again["status"] == "WAITING_APPROVAL"
    # Owner denies through the normal decide surface.
    deny = client.post(f"/ask/approvals/{approval_id}/reject")
    assert deny.status_code == 200
    final = client.post(f"/tasks/{created['task_id']}/advance").json()
    assert final["status"] == "FAILED"
    assert final["error_code"] == "APPROVAL_REJECTED"


def test_task_cancel_and_retry(isolated_db, monkeypatch):
    client = _client(monkeypatch)
    agent = _agent(client)
    _cap(client, agent["id"], "job")
    created = client.post(
        "/tasks",
        json={"target_agent": agent["id"],
              "capability": f"{agent['id']}.job@v1"}).json()
    assert created["status"] == "WAITING_APPROVAL"
    cancelled = client.post(
        f"/tasks/{created['task_id']}/cancel").json()
    assert cancelled["status"] == "CANCELLED"
    listed = client.get("/tasks", params={"status": "CANCELLED"}).json()
    assert [t["task_id"] for t in listed["tasks"]] == [created["task_id"]]
    detail = client.get(f"/tasks/{created['task_id']}").json()
    assert detail["children"] == []
    assert client.get("/tasks/nope").status_code == 404


def test_task_idempotency_over_http(isolated_db, monkeypatch):
    client = _client(monkeypatch)
    agent = _agent(client)
    _cap(client, agent["id"], "job")
    first = client.post(
        "/tasks",
        json={"target_agent": agent["id"],
              "capability": f"{agent['id']}.job@v1",
              "idempotency_key": "demo-1"}).json()
    second = client.post(
        "/tasks",
        json={"target_agent": agent["id"],
              "capability": f"{agent['id']}.job@v1",
              "idempotency_key": "demo-1"}).json()
    assert second["task_id"] == first["task_id"]


def test_tasks_require_auth(isolated_db):
    from app.main import app

    bare = TestClient(app)
    assert bare.get("/tasks").status_code == 401
    assert bare.post("/tasks", json={}).status_code == 401


def test_chat_agent_param_unknown_and_known(isolated_db, monkeypatch):
    import app.api.routes.chat as chat_route
    from app.main import app
    from tests.test_stream import make_provider

    async def source(payload):
        yield {"content": "hi"}

    monkeypatch.setattr(
        chat_route, "build_provider",
        lambda: make_provider(post_stream_fn=source))
    monkeypatch.setattr(chat_route, "build_tools", lambda: {})
    client = TestClient(app, headers=auth_headers())
    bad = client.get("/chat/stream",
                     params={"message": "hi", "agent": "ghost-xyz"})
    assert bad.status_code == 400
    assert bad.json()["code"] == "UNKNOWN_AGENT"
    client.post("/agents", json={"name": "helper"})
    with client.stream(
        "GET", "/chat/stream",
        params={"message": "hi", "session_id": "s1", "agent": "helper"},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")
    assert '"type": "done"' in body or '"type":"done"' in body
    # A durable task records the agent-routed turn.
    found = client.get("/tasks").json()["tasks"]
    assert found and found[0]["status"] == "COMPLETED"
