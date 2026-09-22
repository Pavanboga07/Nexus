"""V6 long-term memory tests (TDD red first).

Contract from the plan:
- extraction stores facts (scripted extractor, real SQLite + sqlite-vec)
- recall blends vector + FTS keyword match (blend documented in store)
- forget deletes + audits; query after delete returns nothing
- cross-session recall in a new session
- export/import round-trip (portable JSON)
- gate: fact stored Monday recalled Friday across sessions (frozen
  timestamps, no sleeps); background extraction never breaks chat.
"""

from __future__ import annotations

import json

import pytest

from app.memory.extract import extract_facts, queue_extraction, record_turn
from app.memory.store import MemoryStore


@pytest.fixture()
def store_path(tmp_path):
    return str(tmp_path / "memory.db")


@pytest.fixture()
def memstore(store_path):
    return MemoryStore(store_path)


def test_extraction_stores_facts(memstore):
    facts = extract_facts(
        "Remember that my dog is called Biscuit.",
        "",
    )
    assert len(facts) >= 1
    ids = memstore.add_many(
        facts, session_id="s1", created_at="2026-09-22T09:00:00+00:00"
    )
    assert len(ids) == len(facts)
    assert len(memstore.list_all()) == len(facts)


def test_recall_blends_vector_and_fts(memstore):
    memstore.add(
        "My dog is called Biscuit.",
        session_id="s1",
        created_at="2026-09-22T09:00:00+00:00",
    )
    memstore.add(
        "The capital of France is Paris.",
        session_id="s1",
        created_at="2026-09-22T09:00:00+00:00",
    )
    # Keyword leg: exact rare token must surface the right fact.
    hits = memstore.recall("Biscuit", limit=5)
    assert any("Biscuit" in h["text"] for h in hits)
    # Vector leg: paraphrase with no shared rare token still recalls.
    hits = memstore.recall("what is my puppy named", limit=5)
    texts = [h["text"] for h in hits]
    assert any("Biscuit" in t for t in texts)


def test_forget_deletes_and_audits(memstore):
    mid = memstore.add(
        "My dog is called Biscuit.",
        session_id="s1",
        created_at="2026-09-22T09:00:00+00:00",
    )
    assert memstore.recall("Biscuit", limit=5)
    assert memstore.forget(mid) is True
    # Provably gone: vector leg and keyword leg both return nothing.
    assert memstore.recall("Biscuit", limit=5) == []
    assert memstore.list_all() == []
    audits = memstore.audit_log()
    assert any(a["memory_id"] == mid and a["action"] == "forget" for a in audits)
    assert memstore.forget("no-such-id") is False


def test_cross_session_recall_in_new_session(store_path):
    first = MemoryStore(store_path)
    first.add(
        "My dog is called Biscuit.",
        session_id="monday-session",
        created_at="2026-09-22T09:00:00+00:00",
    )
    # A brand-new store handle (new session) recalls the old fact.
    second = MemoryStore(store_path)
    hits = second.recall("Biscuit", limit=5)
    assert any("Biscuit" in h["text"] for h in hits)


def test_monday_friday_gate(store_path):
    monday = MemoryStore(store_path)
    monday.add(
        "Friday standup moved to 3pm.",
        session_id="monday",
        created_at="2026-09-22T09:00:00+00:00",  # Monday (frozen)
    )
    friday = MemoryStore(store_path)  # new session days later
    hits = friday.recall(
        "when is standup",
        limit=5,
        now="2026-09-26T09:00:00+00:00",  # Friday (frozen, no sleeps)
    )
    assert any("3pm" in h["text"] for h in hits)


def test_export_import_roundtrip(memstore, tmp_path):
    memstore.add(
        "My dog is called Biscuit.",
        session_id="s1",
        created_at="2026-09-22T09:00:00+00:00",
    )
    path = str(tmp_path / "memories.json")
    memstore.export_json(path)
    payload = json.loads(open(path, encoding="utf-8").read())
    assert isinstance(payload, list) and len(payload) == 1
    assert payload[0]["text"] == "My dog is called Biscuit."
    assert payload[0]["id"]

    fresh = MemoryStore(str(tmp_path / "fresh.db"))
    n = fresh.import_json(path)
    assert n == 1
    assert any("Biscuit" in h["text"] for h in fresh.recall("Biscuit"))


def test_background_extraction_never_breaks_chat(store_path):
    # A failing extractor must not raise out of the fire-and-forget path.
    def boom(user_text, assistant_text):
        raise RuntimeError("extractor exploded")

    thread = queue_extraction(
        store_path,
        user_text="hello",
        assistant_text="hi",
        session_id="s1",
        created_at="2026-09-22T09:00:00+00:00",
        extractor=boom,
    )
    thread.join(timeout=10)
    assert MemoryStore(store_path).list_all() == []

    # The happy path stores facts without the caller waiting on recall.
    thread = record_turn(
        store_path,
        user_text="Remember that my dog is called Biscuit.",
        assistant_text="Noted!",
        session_id="s1",
        created_at="2026-09-22T09:00:00+00:00",
        background=True,
    )
    thread.join(timeout=10)
    assert any(
        "Biscuit" in h["text"]
        for h in MemoryStore(store_path).recall("Biscuit")
    )


# SECTION: HTTP API (memory browser backend)


def _client(db_path, monkeypatch):
    from app.main import app

    monkeypatch.setenv("NEXUS_DB_PATH", db_path)
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_memory_api_search_forget_export_import(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    MemoryStore(db).add(
        "My dog is called Biscuit.",
        session_id="s1",
        created_at="2026-09-22T09:00:00+00:00",
    )
    client = _client(db, monkeypatch)

    found = client.get("/memory", params={"q": "Biscuit"}).json()["memories"]
    assert any("Biscuit" in m["text"] for m in found)

    mid = found[0]["id"]
    assert client.delete(f"/memory/{mid}").status_code == 200
    assert client.delete(f"/memory/{mid}").status_code == 404
    # Forget provably gone over HTTP too.
    assert client.get("/memory", params={"q": "Biscuit"}).json() == {
        "memories": []
    }

    exported = client.get("/memory/export").json()["memories"]
    assert exported == []
    MemoryStore(db).add(
        "Friday standup moved to 3pm.",
        session_id="s1",
        created_at="2026-09-22T09:00:00+00:00",
    )
    exported = client.get("/memory/export").json()["memories"]
    assert len(exported) == 1
    resp = client.post("/memory/import", json={"memories": exported})
    assert resp.json() == {"imported": 1}  # idempotent re-import
    assert client.post(
        "/memory/import", json={"memories": [{"nonsense": 1}]}
    ).status_code == 400


def test_chat_stream_extracts_without_breaking(tmp_path, monkeypatch):
    import time

    import app.api.routes.chat as chat_route

    db = str(tmp_path / "chat.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)

    async def source(payload):
        yield {"content": "noted that"}

    from tests.test_stream import make_provider

    monkeypatch.setattr(
        chat_route, "build_provider", lambda: make_provider(post_stream_fn=source)
    )
    monkeypatch.setattr(chat_route, "build_tools", lambda: {})

    from app.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    # The user turn carries a fact; extraction must not break the stream.
    with client.stream(
        "GET",
        "/chat/stream",
        params={"message": "Remember that my dog is called Biscuit"},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")
    assert '"type": "done"' in body or '"type":"done"' in body

    deadline = time.time() + 10
    texts: list[str] = []
    while time.time() < deadline:
        texts = [m["text"] for m in MemoryStore(db).list_all()]
        if any("Biscuit" in t for t in texts):
            break
    assert any("Biscuit" in t for t in texts)
