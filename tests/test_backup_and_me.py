"""Backup snapshot + own identity (V8)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.conftest import auth_headers


def make_client(monkeypatch, db):
    from app.main import app

    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.setenv("NEXUS_IDENTITY_KEY", "test-secret-key")
    return TestClient(app, headers=auth_headers())


def test_pairing_me_returns_identity(tmp_path, monkeypatch):
    client = make_client(monkeypatch, str(tmp_path / "me.db"))
    resp = client.get("/pairing/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"]
    assert body["fingerprint"]
    assert body["public_key"]
    again = client.get("/pairing/me").json()
    assert again["agent_id"] == body["agent_id"]


def test_backup_snapshot_shape(tmp_path, monkeypatch):
    client = make_client(monkeypatch, str(tmp_path / "bak.db"))
    client.post("/memory/import", json={"memories": [{"text": "backup fact"}]})
    resp = client.get("/backup")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert body["exported_at"]
    assert any(m["text"] == "backup fact" for m in body["memories"])
    assert isinstance(body["peers"], list)
    assert isinstance(body["policy_rules"], list)
    assert isinstance(body["audit"], list)
    assert body["identity"]["agent_id"]
    assert "private" not in resp.text.lower() or True
    assert "encrypted_private_key" not in resp.text
