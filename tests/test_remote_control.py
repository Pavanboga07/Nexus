"""Remote control traffic: cancel/status actions on live tasks."""

from __future__ import annotations

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


SECRET = "remote-control-test-secret-xxxxxxx"


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


def _pair(conn):
    """Two local agents treated as task parties (same-owner control)."""
    from app import agents

    a = agents.create_agent(conn, SECRET, "anna")
    b = agents.create_agent(conn, SECRET, "ben")
    return a, b


def _control(conn, sender_priv, sender_id, recipient_id, action,
             correlation, extra=None):
    from app.a2a import envelope as app_envelope

    payload = {"action": action}
    payload.update(extra or {})
    env = app_envelope.sign(
        app_envelope.new_envelope(
            sender=sender_id, recipient=recipient_id,
            message_type="request", payload=payload,
            correlation_id=correlation),
        sender_priv)
    return env


def _keys(conn, handle):
    from app import agents
    from app.identity import crypto

    priv, _ = agents.resolve_signing_key(conn, SECRET, handle)
    return priv


def test_remote_cancel_lands_on_live_task(db_env):
    from app import tasks
    from app.a2a import service

    conn = _conn(db_env)
    try:
        a, b = _pair(conn)
        task = tasks.create_task(
            conn, requesting_agent_id=a["agent_id"],
            target_agent=b["agent_id"])
        tasks.transition(conn, task["task_id"], "RESOLVING")
        tasks.transition(conn, task["task_id"], "AUTHORIZED")
        tasks.transition(conn, task["task_id"], "DISPATCHED")
        env = _control(
            conn, _keys(conn, "anna"), a["agent_id"], b["agent_id"],
            "task_cancel", task["correlation_id"])
        out = service.receive_envelope(
            conn, env, signer_priv=_keys(conn, "ben"),
            local_id=b["agent_id"])
        assert out["outcome"] == "cancelled"
        assert tasks.get_task(
            conn, task["task_id"])["status"] == "CANCELLED"
    finally:
        conn.close()


def test_remote_cancel_by_stranger_ignored(db_env):
    from app import agents, tasks
    from app.a2a import service

    conn = _conn(db_env)
    try:
        a, b = _pair(conn)
        agents.create_agent(conn, SECRET, "mallory")
        task = tasks.create_task(
            conn, requesting_agent_id=a["agent_id"],
            target_agent=b["agent_id"])
        env = _control(
            conn, _keys(conn, "mallory"),
            agents.get_agent(conn, "mallory")["agent_id"],
            b["agent_id"], "task_cancel", task["correlation_id"])
        out = service.receive_envelope(
            conn, env, signer_priv=_keys(conn, "ben"),
            local_id=b["agent_id"])
        assert out["outcome"] == "ignored"
        assert tasks.get_task(
            conn, task["task_id"])["status"] == "PENDING"
    finally:
        conn.close()


def test_remote_cancel_terminal_ignored(db_env):
    from app import tasks
    from app.a2a import service

    conn = _conn(db_env)
    try:
        a, b = _pair(conn)
        task = tasks.create_task(
            conn, requesting_agent_id=a["agent_id"],
            target_agent=b["agent_id"])
        tasks.complete_task(conn, task["task_id"], {"ok": 1})
        env = _control(
            conn, _keys(conn, "anna"), a["agent_id"], b["agent_id"],
            "task_cancel", task["correlation_id"])
        out = service.receive_envelope(
            conn, env, signer_priv=_keys(conn, "ben"),
            local_id=b["agent_id"])
        assert out["outcome"] == "ignored"
    finally:
        conn.close()


def test_remote_status_recorded_not_executed(db_env):
    from app import tasks
    from app.a2a import service

    conn = _conn(db_env)
    try:
        a, b = _pair(conn)
        task = tasks.create_task(
            conn, requesting_agent_id=a["agent_id"],
            target_agent=b["agent_id"])
        tasks.transition(conn, task["task_id"], "RESOLVING")
        tasks.transition(conn, task["task_id"], "AUTHORIZED")
        tasks.transition(conn, task["task_id"], "DISPATCHED")
        env = _control(
            conn, _keys(conn, "anna"), a["agent_id"], b["agent_id"],
            "task_status", task["correlation_id"],
            {"remote_status": "RUNNING", "detail": "warming up"})
        out = service.receive_envelope(
            conn, env, signer_priv=_keys(conn, "ben"),
            local_id=b["agent_id"])
        assert out["outcome"] == "noted"
        # Advisory only: task state untouched, event recorded.
        assert tasks.get_task(
            conn, task["task_id"])["status"] == "DISPATCHED"
        events = [e["event"] for e in tasks.task_events(
            conn, task["task_id"])]
        assert "TASK_REMOTE_RUNNING" in events
    finally:
        conn.close()


def test_status_from_stranger_ignored(db_env):
    from app import agents, tasks
    from app.a2a import service

    conn = _conn(db_env)
    try:
        a, b = _pair(conn)
        agents.create_agent(conn, SECRET, "mallory")
        task = tasks.create_task(
            conn, requesting_agent_id=a["agent_id"],
            target_agent=b["agent_id"])
        env = _control(
            conn, _keys(conn, "mallory"),
            agents.get_agent(conn, "mallory")["agent_id"],
            b["agent_id"], "task_status", task["correlation_id"],
            {"remote_status": "COMPLETED"})
        out = service.receive_envelope(
            conn, env, signer_priv=_keys(conn, "ben"),
            local_id=b["agent_id"])
        assert out["outcome"] == "ignored"
        events = [e["event"] for e in tasks.task_events(
            conn, task["task_id"])]
        assert not [e for e in events if e.startswith("TASK_REMOTE_")]
    finally:
        conn.close()
