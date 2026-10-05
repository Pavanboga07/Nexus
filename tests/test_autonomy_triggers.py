"""Event triggers: matching, gating, loop prevention, idempotency."""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return db


SECRET = "trigger-test-secret-xxxxxxxxxxxx"
CALLS: list = []


async def _fake_execute(tools, name, args):
    CALLS.append((name, dict(args)))
    return json.dumps({"result": "triggered-ok"})


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


@pytest.fixture
def world(db_env, monkeypatch):
    from app import agents, capabilities
    from app.a2a import service

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    CALLS.clear()
    conn = _conn(db_env)
    agents.create_agent(conn, SECRET, "watcher")
    agents.create_agent(conn, SECRET, "worker")
    capabilities.register_capability(
        conn, "worker", "job", tool="web_search")
    service.create_rule(conn, peer="*", action="*")
    yield conn
    conn.close()


def _run(coro):
    return asyncio.run(coro)


def _grant_watcher_to_worker(conn):
    from app import delegations

    return delegations.issue_grant(
        conn, SECRET, "watcher",
        _agent_crypto(conn, "worker"), "worker.job@v1", "answer",
        ttl_seconds=3600)


def _trigger_with_grant(conn, **kw):
    from app import autonomy

    grant = _grant_watcher_to_worker(conn)
    params = {"agent_id": "watcher", "event_type": "task.completed",
              "target_agent": "worker",
              "target_capability": "worker.job@v1",
              "delegation_id": grant["id"]}
    params.update(kw)
    return autonomy.create_trigger(conn, **params)


def _agent_crypto(conn, handle):
    from app import agents

    return agents.get_agent(conn, handle)["agent_id"]


def test_trigger_fires_on_matching_event(world):
    from app import autonomy

    conn = world
    grant = _grant_watcher_to_worker(conn)
    trigger = autonomy.create_trigger(
        conn, agent_id="watcher", event_type="task.completed",
        target_agent="worker", target_capability="worker.job@v1",
        delegation_id=grant["id"],
        event_filter={"capability_id": "worker.job@v1"})
    event = autonomy.emit_event(
        conn, "task.completed",
        {"capability_id": "worker.job@v1", "task_id": "tsk_x"})
    fired = _run(autonomy.process_events(conn))
    assert len(fired) == 1
    assert fired[0]["origin"] == "trigger"
    assert fired[0]["trigger_id"] == trigger["id"]
    assert fired[0]["status"] == "COMPLETED"
    assert CALLS and CALLS[0][0] == "web_search"


def test_filter_mismatch_does_not_fire(world):
    from app import autonomy

    conn = world
    autonomy.create_trigger(
        conn, agent_id="watcher", event_type="task.completed",
        target_agent="worker", target_capability="worker.job@v1",
        event_filter={"capability_id": "other.cap@v1"})
    autonomy.emit_event(conn, "task.completed", {"capability_id": "x"})
    assert _run(autonomy.process_events(conn)) == []
    assert CALLS == []


def test_disabled_trigger_ignored(world):
    from app import autonomy

    conn = world
    trigger = autonomy.create_trigger(
        conn, agent_id="watcher", event_type="task.completed",
        target_agent="worker", target_capability="worker.job@v1")
    autonomy.set_trigger_enabled(conn, trigger["id"], False)
    autonomy.emit_event(conn, "task.completed", {})
    assert _run(autonomy.process_events(conn)) == []
    assert CALLS == []


def test_duplicate_event_fires_once(world):
    from app import autonomy
    from app import tasks as task_tracker

    conn = world
    trigger = _trigger_with_grant(conn)
    event = autonomy.emit_event(conn, "custom.ping", {})
    # Separate trigger on a non-cascading event type.
    lone = autonomy.create_trigger(
        conn, agent_id="watcher", event_type="custom.ping",
        target_agent="worker", target_capability="worker.job@v1",
        delegation_id=_grant_watcher_to_worker(conn)["id"])
    _ = trigger
    first = _run(autonomy.process_events(conn))
    mine = [t for t in first if t.get("trigger_id") == lone["id"]]
    assert len(mine) == 1
    second = _run(autonomy.process_events(conn))
    mine2 = [t for t in second if t.get("trigger_id") == lone["id"]]
    assert len(mine2) == 1
    assert mine2[0]["task_id"] == mine[0]["task_id"]
    # The reprocess fired nothing new for this event (other triggers
    # may legitimately cascade off the completion — bounded by depth).
    _ = event


def test_trigger_depth_bounded(world):
    from app import autonomy

    conn = world
    autonomy.create_trigger(
        conn, agent_id="watcher", event_type="task.completed",
        target_agent="worker", target_capability="worker.job@v1")
    deep = autonomy.emit_event(conn, "task.completed", {}, depth=99)
    fired = _run(autonomy.process_events(conn))
    assert fired == []
    _ = deep


def test_trigger_validates_targets(world):
    from app import autonomy

    conn = world
    with pytest.raises(autonomy.AutonomyError):
        autonomy.create_trigger(conn, agent_id="watcher", event_type="x")
    with pytest.raises(autonomy.AutonomyError):
        autonomy.create_trigger(
            conn, agent_id="watcher", event_type="x",
            target_workflow_id="wfl_1", target_agent="worker")
    with pytest.raises(autonomy.AutonomyError):
        autonomy.create_trigger(conn, agent_id="ghost",
                                event_type="x",
                                target_agent="worker",
                                target_capability="worker.job@v1")


def test_trigger_http_crud(db_env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import auth_headers

    client = TestClient(app, headers=auth_headers())
    client.post("/agents", json={"name": "tw"})
    created = client.post(
        "/autonomy/triggers",
        json={"agent_id": "tw", "event_type": "task.completed",
              "target_agent": "tw",
              "target_capability": "tw.job@v1"}).json()
    assert created["event_type"] == "task.completed"
    assert len(client.get("/autonomy/triggers").json()["triggers"]) == 1
    assert client.post(
        f"/autonomy/triggers/{created['id']}/disable").json()[
            "enabled"] is False
    assert client.delete(
        f"/autonomy/triggers/{created['id']}").json()["deleted"] is True
