"""Item 3: recall injection with session scope (TDD).

``GET /chat/stream`` takes ``session_id``: the turn recalls session
memories into the system prompt before generation (scoped per session)
and extraction records the same ``session_id`` on stored rows.
"""

from __future__ import annotations

import time

from app.llm.provider import SYSTEM_PROMPT

AT = "2026-09-22T09:00:00+00:00"


def _seed(db):
    from app.memory.store import MemoryStore

    store = MemoryStore(db)
    try:
        store.add("My dog is called Biscuit.", session_id="A",
                  created_at=AT)
        store.add("The capital of France is Paris.", session_id="B",
                  created_at=AT)
    finally:
        store.close()


def _stream_client(monkeypatch, db, seen):
    import app.api.routes.chat as chat_route
    from app.main import app
    from fastapi.testclient import TestClient
    from tests.test_stream import make_provider

    monkeypatch.setenv("NEXUS_DB_PATH", db)

    async def source(payload):
        seen["messages"] = [dict(m) for m in payload["messages"]]
        yield {"content": "noted that"}

    monkeypatch.setattr(
        chat_route, "build_provider",
        lambda: make_provider(post_stream_fn=source),
    )
    monkeypatch.setattr(chat_route, "build_tools", lambda: {})
    return TestClient(app)


def test_recall_session_scope_filters_store(tmp_path):
    from app.memory.store import MemoryStore

    db = str(tmp_path / "scoped.db")
    _seed(db)
    store = MemoryStore(db)
    try:
        hits_a = store.recall("Biscuit", limit=5, session_id="A")
        assert any("Biscuit" in h["text"] for h in hits_a)
        assert all(h["session_id"] == "A" for h in hits_a)
        hits_b = store.recall("Biscuit", limit=5, session_id="B")
        assert all(h["session_id"] == "B" for h in hits_b)
        assert not any("Biscuit" in h["text"] for h in hits_b)
        # Unscoped default keeps the memory-browser behavior.
        assert any(
            "Biscuit" in h["text"] for h in store.recall("Biscuit", limit=5)
        )
    finally:
        store.close()


def test_stream_recalls_session_fact_in_system_prompt(tmp_path, monkeypatch):
    db = str(tmp_path / "chat_a.db")
    _seed(db)
    seen: dict = {}
    client = _stream_client(monkeypatch, db, seen)
    resp = client.get(
        "/chat/stream",
        params={"message": "what is my dog called", "session_id": "A"},
    )
    assert resp.status_code == 200, resp.text
    system = seen["messages"][0]["content"]
    assert seen["messages"][0]["role"] == "system"
    assert "Biscuit" in system


def test_stream_does_not_recall_other_session(tmp_path, monkeypatch):
    db = str(tmp_path / "chat_b.db")
    _seed(db)
    seen: dict = {}
    client = _stream_client(monkeypatch, db, seen)
    resp = client.get(
        "/chat/stream",
        params={"message": "what is my dog called", "session_id": "B"},
    )
    assert resp.status_code == 200, resp.text
    system = seen["messages"][0]["content"]
    assert "Biscuit" not in system


def test_extraction_records_session_id(tmp_path, monkeypatch):
    from app.memory.store import MemoryStore

    db = str(tmp_path / "chat_x.db")
    seen: dict = {}
    client = _stream_client(monkeypatch, db, seen)
    resp = client.get(
        "/chat/stream",
        params={
            "message": "Remember that my dog is called Biscuit",
            "session_id": "chat-session-1",
        },
    )
    assert resp.status_code == 200, resp.text
    deadline = time.time() + 10
    rows: list = []
    while time.time() < deadline:
        rows = MemoryStore(db).list_all()
        if any("Biscuit" in r["text"] for r in rows):
            break
    assert any("Biscuit" in r["text"] for r in rows)
    assert {
        r["session_id"] for r in rows if "Biscuit" in r["text"]
    } == {"chat-session-1"}


def test_stream_slow_recall_falls_back_to_bare_prompt(tmp_path, monkeypatch):
    """Recall exceeding 1.0s must not hold the turn: bounded elapsed."""
    from app.memory.store import MemoryStore

    db = str(tmp_path / "chat_slow.db")
    _seed(db)
    seen: dict = {}
    client = _stream_client(monkeypatch, db, seen)

    def slow_recall(self, *args, **kwargs):
        time.sleep(5)
        return [{"text": "My dog is called Biscuit."}]

    monkeypatch.setattr(MemoryStore, "recall", slow_recall)
    # Shared portal: a bare TestClient opens a fresh blocking portal per
    # request whose teardown joins the timed-out worker thread
    # (asyncio shutdown_default_executor), masking the turn latency
    # this test bounds. Production (uvicorn) reuses one loop, so the
    # orphaned worker never holds a turn there either.
    with client:
        start = time.monotonic()
        resp = client.get(
            "/chat/stream",
            params={"message": "what is my dog called", "session_id": "A"},
        )
        elapsed = time.monotonic() - start
        assert resp.status_code == 200, resp.text
        assert elapsed < 4.0, f"recall blocked the turn for {elapsed:.1f}s"
        assert seen["messages"][0]["role"] == "system"
        assert seen["messages"][0]["content"] == SYSTEM_PROMPT
        assert '"type": "done"' in resp.text or '"type":"done"' in resp.text


def test_stream_recall_error_falls_back_to_bare_prompt(tmp_path, monkeypatch):
    """Recall raising must not affect the turn (fail-open)."""
    from app.memory.store import MemoryStore

    db = str(tmp_path / "chat_boom.db")
    _seed(db)
    seen: dict = {}
    client = _stream_client(monkeypatch, db, seen)

    def boom(self, *args, **kwargs):
        raise RuntimeError("recall exploded")

    monkeypatch.setattr(MemoryStore, "recall", boom)
    resp = client.get(
        "/chat/stream",
        params={"message": "what is my dog called", "session_id": "A"},
    )
    assert resp.status_code == 200, resp.text
    assert seen["messages"][0]["role"] == "system"
    assert seen["messages"][0]["content"] == SYSTEM_PROMPT
    assert '"type": "done"' in resp.text or '"type":"done"' in resp.text
