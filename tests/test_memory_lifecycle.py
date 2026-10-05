"""Memory lifecycle: TTL expiry, sweep, archive-by-export, isolation."""

from __future__ import annotations


def _store(tmp_path, name="lc.db"):
    from app.memory.store import MemoryStore

    return MemoryStore(str(tmp_path / name))


def test_ttl_expired_hidden_and_swept(tmp_path):
    store = _store(tmp_path)
    try:
        live = store.add("permanent fact", agent_id="alice")
        dying = store.add("temporary fact", agent_id="alice",
                          expires_at="2000-01-01T00:00:00Z")
        future = store.add("future fact", agent_id="alice",
                           expires_at="2030-01-01T00:00:00Z")
        assert [m["id"] for m in store.list_all(agent_id="alice")] == [
            live, future]
        hits = store.recall("fact", agent_id="alice",
                            now="2026-10-04T00:00:00Z")
        assert {h["id"] for h in hits} == {live, future}
        assert store.forget_expired(now="2026-10-04T00:00:00Z") == 1
        assert [m["id"] for m in store.list_all(agent_id="alice",
                                                now="2026-10-04T00:00:00Z")] == [
            live, future]
        _ = dying
    finally:
        store.close()


def test_expiry_is_per_agent(tmp_path):
    store = _store(tmp_path)
    try:
        store.add("shared wording here", agent_id="alice",
                  expires_at="2000-01-01T00:00:00Z")
        store.add("shared wording here", agent_id="bob")
        assert store.list_all(agent_id="alice") == []
        assert len(store.list_all(agent_id="bob")) == 1
        assert store.forget_expired() == 1
    finally:
        store.close()


def test_archive_then_forget(tmp_path):
    store = _store(tmp_path)
    try:
        store.add("archive me", agent_id="alice")
        assert store.export_json(str(tmp_path / "a.json"),
                                 agent_id="alice") == 1
        mid = store.list_all(agent_id="alice")[0]["id"]
        assert store.forget(mid) is True
        assert store.list_all(agent_id="alice") == []
        audit = [a for a in store.audit_log()
                 if a["memory_id"] == mid]
        assert [a["action"] for a in audit] == ["forget"]
    finally:
        store.close()
