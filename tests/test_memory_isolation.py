"""Agent memory isolation: partitioned recall, listing, and import.

Agent A must never retrieve Agent B's rows through recall or listing.
Legacy rows (no agent) belong to the default agent.
"""

from __future__ import annotations

from app.memory.store import MemoryStore


def _store(tmp_path, name="iso.db"):
    return MemoryStore(str(tmp_path / name))


def test_recall_partitioned_by_agent(tmp_path):
    store = _store(tmp_path)
    try:
        store.add("Alice keeps bees in the garden", agent_id="alice")
        store.add("Bob repairs vintage radios", agent_id="bob")
        alice_hits = store.recall("bees garden", agent_id="alice")
        assert any("bees" in h["text"] for h in alice_hits)
        assert all(h["agent_id"] == "alice" for h in alice_hits)
        # Same query as Bob finds nothing of Alice's.
        bob_hits = store.recall("bees garden", agent_id="bob")
        assert all(h["agent_id"] == "bob" for h in bob_hits)
        assert not any("bees" in h["text"] for h in bob_hits)
    finally:
        store.close()


def test_list_all_partitioned(tmp_path):
    store = _store(tmp_path)
    try:
        store.add("alice fact", agent_id="alice")
        store.add("bob fact", agent_id="bob")
        assert [m["text"] for m in store.list_all(agent_id="alice")] == [
            "alice fact"
        ]
        assert [m["text"] for m in store.list_all(agent_id="bob")] == [
            "bob fact"
        ]
        assert store.list_all() == []
    finally:
        store.close()


def test_legacy_rows_belong_to_default(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, text TEXT,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO memories (id, text) VALUES ('m1', 'legacy fact')"
    )
    conn.commit()
    conn.close()
    store = MemoryStore(path)
    try:
        # Raw legacy rows surface under the default agent only (recall
        # needs indexed rows, which only add() creates — out of scope).
        assert [m["text"] for m in store.list_all()] == ["legacy fact"]
        assert store.list_all(agent_id="alice") == []
    finally:
        store.close()


def test_session_and_agent_compose(tmp_path):
    store = _store(tmp_path)
    try:
        store.add("session scoped", session_id="s1", agent_id="alice")
        store.add("other session", session_id="s2", agent_id="alice")
        hits = store.recall(
            "session scoped", session_id="s1", agent_id="alice"
        )
        assert any(h["text"] == "session scoped" for h in hits)
        assert all(h["session_id"] == "s1" for h in hits)
    finally:
        store.close()


def test_update_preserves_owner(tmp_path):
    store = _store(tmp_path)
    try:
        mid = store.add("original", agent_id="alice")
        assert store.update(mid, "edited") is True
        rows = store.list_all(agent_id="alice")
        assert [(m["id"], m["text"]) for m in rows] == [(mid, "edited")]
        assert store.list_all(agent_id="bob") == []
    finally:
        store.close()


def test_add_defaults_to_default_agent(tmp_path):
    store = _store(tmp_path)
    try:
        mid = store.add("no agent given")
        rows = store.list_all()
        assert [(m["id"], m["agent_id"]) for m in rows] == [(mid, "default")]
    finally:
        store.close()


def test_export_round_trip_keeps_agent(tmp_path):
    store = _store(tmp_path, "exp.db")
    try:
        store.add("portable fact", agent_id="alice")
        assert (
            store.export_json(str(tmp_path / "out.json"), agent_id="alice")
            == 1
        )
        second = MemoryStore(str(tmp_path / "exp2.db"))
        try:
            import json

            payload = json.loads((tmp_path / "out.json").read_text())
            assert payload[0]["agent_id"] == "alice"
            assert second.import_json(str(tmp_path / "out.json")) == 1
            assert [m["text"] for m in second.list_all(agent_id="alice")] == [
                "portable fact"
            ]
            assert second.list_all() == []
        finally:
            second.close()
    finally:
        store.close()
