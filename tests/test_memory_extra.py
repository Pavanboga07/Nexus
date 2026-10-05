"""Memory extras (V8): edit, bulk delete, audit log endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.conftest import auth_headers


def make_client(monkeypatch, db):
    from app.main import app

    monkeypatch.setenv("NEXUS_DB_PATH", db)
    return TestClient(app, headers=auth_headers())


def seed(client, texts):
    ids = []
    resp = client.post("/memory/import", json={"memories": [{"text": t} for t in texts]})
    assert resp.status_code == 200
    listed = client.get("/memory").json()["memories"]
    assert len(listed) == len(texts)
    return [m["id"] for m in listed]


def test_update_rewrites_text_and_reindex(tmp_path, monkeypatch):
    import sqlite3

    import sqlite_vec

    db = str(tmp_path / "me.db")
    client = make_client(monkeypatch, db)
    (mid,) = seed(client, ["my dog is called Biscuit"])
    resp = client.put(f"/memory/{mid}", json={"text": "my cat is called Tom"})
    assert resp.status_code == 200
    assert resp.json() == {"id": mid, "updated": True}
    listed = client.get("/memory").json()["memories"]
    assert [m["text"] for m in listed] == ["my cat is called Tom"]
    hits = client.get("/memory", params={"q": "Tom"}).json()["memories"]
    assert any(
        m["id"] == mid and m["text"] == "my cat is called Tom" for m in hits
    )
    # Index rows rebuilt, not duplicated: one vec row, and the FTS
    # index no longer contains the old wording.
    conn = sqlite3.connect(db)
    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    try:
        vec_n = conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0]
        assert vec_n == 1
        fts_old = conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH 'Biscuit'"
        ).fetchone()[0]
        assert fts_old == 0
        fts_new = conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH 'Tom'"
        ).fetchone()[0]
        assert fts_new == 1
    finally:
        conn.close()


def test_update_validates(tmp_path, monkeypatch):
    client = make_client(monkeypatch, str(tmp_path / "me2.db"))
    (mid,) = seed(client, ["something"])
    assert client.put(f"/memory/{mid}", json={"text": "  "}).status_code == 400
    assert client.put("/memory/nope", json={"text": "x"}).status_code == 404


def test_bulk_delete_and_audit(tmp_path, monkeypatch):
    client = make_client(monkeypatch, str(tmp_path / "me3.db"))
    ids = seed(client, ["one", "two", "three"])
    resp = client.post(
        "/memory/bulk-delete", json={"ids": ids[:2] + ["missing-id"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["forgotten"] == 2
    assert body["missing"] == ["missing-id"]
    remaining = client.get("/memory").json()["memories"]
    assert [m["text"] for m in remaining] == ["three"]

    audit = client.get("/memory/audit").json()["audit"]
    actions = [(a["action"], a["memory_id"]) for a in audit]
    assert (("forget", ids[0]) in actions) and (("forget", ids[1]) in actions)
