"""Per-agent chat sessions: ownership, isolation, history scoping."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return db


SECRET = "session-test-secret-xxxxxxxxxxxx"


def test_thread_owned_by_streaming_agent(db_env, monkeypatch):
    from app.agent import context
    from app import agents
    from app.store import migrate, open_db

    conn = open_db(db_env)
    migrate(conn)
    agents.create_agent(conn, SECRET, "manager")
    agents.create_agent(conn, SECRET, "research")
    conn.close()
    context.save_turn("sess-1", "user", "hello manager",
                      agent_id="manager")
    context.save_turn("sess-1", "assistant", "hi back",
                      agent_id="manager")
    assert context.thread_owner("sess-1") == "manager"
    assert [t["text"] for t in context.load_turns("sess-1")] != []
    # Cross-agent reads return empty, never the other agent's history.
    assert context.load_turns("sess-1", agent_id="research") == []
    assert [t["text"] for t in context.load_turns(
        "sess-1", agent_id="manager")] == ["hello manager", "hi back"]
    assert context.thread_owner("nope") is None


def test_thread_list_scoped_per_agent(db_env, monkeypatch):
    from app.agent import context
    from app import agents
    from app.store import migrate, open_db

    conn = open_db(db_env)
    migrate(conn)
    agents.create_agent(conn, SECRET, "manager")
    agents.create_agent(conn, SECRET, "research")
    conn.close()
    context.save_turn("m-1", "user", "a", agent_id="manager")
    context.save_turn("r-1", "user", "b", agent_id="research")
    assert {t["thread_id"] for t in context.list_threads()} == {"m-1", "r-1"}
    assert [t["thread_id"] for t in context.list_threads(
        agent_id="manager")] == ["m-1"]
    assert [t["thread_id"] for t in context.list_threads(
        agent_id="research")] == ["r-1"]


def test_thread_read_forbidden_across_agents(db_env, monkeypatch):
    from app.agent import context
    from app import agents, owners
    from app.main import app
    from app.store import migrate, open_db

    conn = open_db(db_env)
    migrate(conn)
    try:
        # Create owners first
        owners.create_owner(conn, "manager")
        owners.create_owner(conn, "research")
        # Create agents with the owners
        agents.create_agent(conn, SECRET, "manager", owner_id="manager")
        agents.create_agent(conn, SECRET, "research", owner_id="research")
        # Issue tokens for the agents so they can authenticate
        mgr_token = owners.issue_token(conn, "manager")["token"]
        res_token = owners.issue_token(conn, "research")["token"]
    finally:
        conn.close()

    mgr_headers = {"Authorization": f"Bearer {mgr_token}"}
    res_headers = {"Authorization": f"Bearer {res_token}"}

    context.save_turn("priv-1", "user", "secret", agent_id="manager")
    client = TestClient(app, headers={"Authorization": f"Bearer {mgr_token}"})
    resp = client.get("/chat/threads/priv-1",
                      params={"agent": "research"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"
    mine = client.get("/chat/threads/priv-1",
                      params={"agent": "manager"})
    assert mine.status_code == 200
    legacy = client.get("/chat/threads/priv-1")
    assert legacy.status_code == 200
    scoped = client.get("/chat/threads", params={"agent": "research"})
    assert scoped.json() == {"threads": []}
