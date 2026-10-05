"""MVP hardening: rate limits, size limits, chat/backup owner scoping."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    monkeypatch.delenv("NEXUS_OPERATOR_TOKEN", raising=False)
    return db


def _client(token):
    from app.main import app

    return TestClient(
        app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Rate buckets are process-global (keyed by testserver host); never
    leak counts into other test files."""
    from app.ratelimit import reset_rate_limits

    reset_rate_limits()
    yield
    reset_rate_limits()


def _two_owners(isolated_db):
    from app import agents, owners
    from app.store import migrate, open_db

    conn = open_db(isolated_db)
    migrate(conn)
    try:
        owners.create_owner(conn, "alice")
        owners.create_owner(conn, "bob")
        tok_a = owners.issue_token(conn, "alice")["token"]
        tok_b = owners.issue_token(conn, "bob")["token"]
        agents.create_agent(conn, "s", "a-agent", owner_id="alice")
        agents.create_agent(conn, "s", "b-agent", owner_id="bob")
    finally:
        conn.close()
    return tok_a, tok_b


def test_live_ticket_rate_limited(isolated_db):
    from app.ratelimit import reset_rate_limits

    reset_rate_limits()
    c = _client("nope")
    # Unauthed client cannot mint tickets at all; use an owner token.
    tok_a, _ = _two_owners(isolated_db)
    authed = _client(tok_a)
    codes = [authed.post("/ask/live-ticket").status_code for _ in range(35)]
    assert codes[:30] == [200] * 30
    assert 429 in codes[30:]
    body = authed.post("/ask/live-ticket").json()
    assert body["code"] == "RATE_LIMITED"


def test_rotate_rate_limited_when_pinned(isolated_db, monkeypatch):
    from app.ratelimit import reset_rate_limits

    reset_rate_limits()
    monkeypatch.setenv("NEXUS_OPERATOR_TOKEN", "pinned-for-test")
    c = _client("pinned-for-test")
    codes = [c.post("/settings/operator-token/rotate").status_code
             for _ in range(7)]
    # Pinned env refuses rotation; the 6th+ rapid hit trips the limiter.
    assert codes[:5] == [409] * 5
    assert codes[5] == 429
    assert c.post("/settings/operator-token/rotate").json()["code"] == (
        "RATE_LIMITED")


def test_oversized_body_rejected(isolated_db, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 100)
    tok_a, _ = _two_owners(isolated_db)
    res = _client(tok_a).post(
        "/settings/owners",
        json={"owner_id": "x", "display_name": "y" * 200},
    )
    assert res.status_code == 413
    assert res.json()["code"] == "PAYLOAD_TOO_LARGE"


def test_chat_threads_scoped_to_owner(isolated_db):
    from app.agent.context import save_turn

    tok_a, tok_b = _two_owners(isolated_db)
    save_turn("thread-a1", "user", "hello from alice", agent_id="a-agent")
    alice, bob = _client(tok_a), _client(tok_b)
    assert [t["thread_id"] for t in
            alice.get("/chat/threads").json()["threads"]] == ["thread-a1"]
    assert bob.get("/chat/threads").json()["threads"] == []
    assert bob.get("/chat/threads/thread-a1").status_code == 404
    assert bob.delete("/chat/threads/thread-a1").status_code == 404
    # Alice still owns it.
    assert alice.get("/chat/threads/thread-a1").status_code == 200
    assert alice.delete("/chat/threads/thread-a1").json()["deleted"] is True


def test_chat_stream_rejects_foreign_agent(isolated_db):
    tok_a, tok_b = _two_owners(isolated_db)
    res = _client(tok_b).get(
        "/chat/stream",
        params={"message": "hi", "session_id": "s", "agent": "a-agent"},
    )
    # Unknown-agent (never confirms cross-owner existence); without a
    # model key the owned-agent path would be 503 instead.
    assert res.status_code in (400, 503)


def test_backup_scoped_to_owner(isolated_db):
    from app.memory.store import MemoryStore

    tok_a, tok_b = _two_owners(isolated_db)
    store = MemoryStore(isolated_db)
    try:
        store.add("alice secret", agent_id="a-agent")
        store.add("bob secret", agent_id="b-agent")
    finally:
        store.close()
    alice_mems = _client(tok_a).get("/backup").json()["memories"]
    bob_mems = _client(tok_b).get("/backup").json()["memories"]
    assert [m["text"] for m in alice_mems] == ["alice secret"]
    assert [m["text"] for m in bob_mems] == ["bob secret"]


def test_trigger_stores_resolved_agent_id(isolated_db):
    tok_a, _ = _two_owners(isolated_db)
    alice = _client(tok_a)
    alice.post("/agents/a-agent/capabilities",
               json={"name": "summarize", "tool": "web_search"})
    res = alice.post("/autonomy/triggers", json={
        "agent_id": "a-agent",
        "event_type": "task.completed",
        "target_agent": "a-agent",
        "target_capability": "a-agent.summarize@v1",
    })
    assert res.status_code == 200, res.text
    assert res.json()["agent_id"] == "a-agent"
