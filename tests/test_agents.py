"""Local agent registry: many agents, per-agent keys, rotation history.

Proves the multi-agent identity foundation: default backfill from the
legacy identity row (same keypair, no fork), independent agents with
distinct keys, rotation preserving history, revocation/disable failing
closed, and no private material in any response.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return db


def _secret(monkeypatch, value="agent-test-secret-xxxxxxxxxxxx"):
    monkeypatch.setenv("NEXUS_IDENTITY_KEY", value)
    return value


def _client():
    from app.main import app

    return TestClient(app, headers=auth_headers())


def test_default_backfilled_from_identity(isolated_db, monkeypatch):
    from app import agents
    from app.identity.service import ensure_identity
    from app.machine_config import get_or_create_identity_secret
    from app.store import migrate, open_db

    secret = _secret(monkeypatch)
    conn = open_db(isolated_db)
    migrate(conn)
    ident = ensure_identity(conn, secret)
    view = agents.ensure_default_agent(conn, secret)
    assert view["id"] == "default"
    assert view["agent_id"] == ident.agent_id
    assert view["key_version"] == 1
    assert view["fingerprint"]
    # Idempotent: second call returns the same agent, still version 1.
    again = agents.ensure_default_agent(conn, secret)
    assert again["agent_id"] == ident.agent_id
    assert agents.key_history(conn, "default")[0]["version"] == 1
    conn.close()


def test_default_without_identity_fails_closed(isolated_db, monkeypatch):
    from app import agents
    from app.store import migrate, open_db

    _secret(monkeypatch)
    conn = open_db(isolated_db)
    migrate(conn)
    with pytest.raises(agents.AgentError) as exc:
        agents.ensure_default_agent(conn, "agent-test-secret-xxxxxxxxxxxx")
    assert exc.value.code == "NO_IDENTITY"
    conn.close()


def test_create_list_get_agents(isolated_db, monkeypatch):
    _secret(monkeypatch)
    client = _client()
    assert client.get("/agents").json() == {"agents": []}
    created = client.post(
        "/agents", json={"name": "research", "display_name": "Research"}
    ).json()
    assert created["id"] == "research"
    assert created["owner_id"] == "local"
    assert created["agent_id"].startswith("nexus:ed25519:")
    assert created["status"] == "active"
    assert created["key_version"] == 1
    assert "encrypted_private_key" not in str(created)
    by_id = client.get("/agents/research").json()
    assert by_id["agent_id"] == created["agent_id"]
    by_crypto = client.get(f"/agents/{created['agent_id']}").json()
    assert by_crypto["id"] == "research"
    assert len(client.get("/agents").json()["agents"]) == 1


def test_create_validates_and_dedups(isolated_db, monkeypatch):
    _secret(monkeypatch)
    client = _client()
    assert client.post("/agents", json={"name": "Bad Name!"}).status_code == 400
    assert client.post("/agents", json={"name": "dup"}).status_code == 200
    dup = client.post("/agents", json={"name": "dup"})
    assert dup.status_code == 409
    assert dup.json()["code"] == "ALREADY_EXISTS"
    assert client.get("/agents/nope").status_code == 404


def test_rotate_rolls_key_and_keeps_history(isolated_db, monkeypatch):
    from app.identity import crypto

    _secret(monkeypatch)
    client = _client()
    before = client.post("/agents", json={"name": "ops"}).json()
    after = client.post("/agents/ops/rotate").json()
    assert after["agent_id"] == before["agent_id"]  # same agent, new key
    assert after["key_version"] == 2
    assert after["fingerprint"] != before["fingerprint"]
    keys = client.get("/agents/ops/keys").json()["keys"]
    assert [(k["version"], k["status"]) for k in keys] == [
        (2, "active"),
        (1, "rotated"),
    ]
    # New key actually signs; old material retained but not active.
    assert "encrypted_private_key" not in str(keys)


def test_signing_uses_active_key(isolated_db, monkeypatch):
    from app import agents
    from app.identity import crypto
    from app.store import migrate, open_db

    secret = _secret(monkeypatch)
    conn = open_db(isolated_db)
    migrate(conn)
    created = agents.create_agent(conn, secret, "signer")
    priv1, _ = agents.resolve_signing_key(conn, secret, "signer")
    sig = crypto.sign_bytes(priv1, b"hello")
    assert crypto.verify_bytes(
        crypto.load_public_key(
            __import__("base64").b64decode(
                conn.execute(
                    "SELECT public_key FROM agent_identities WHERE agent_id = ?"
                    " AND status = 'active'",
                    (created["agent_id"],),
                ).fetchone()["public_key"]
            )
        ),
        b"hello",
        sig,
    )
    agents.rotate_agent_key(conn, secret, "signer")
    priv2, _ = agents.resolve_signing_key(conn, secret, "signer")
    sig2 = crypto.sign_bytes(priv2, b"hello again")
    assert sig2 != sig  # different key now signs
    conn.close()


def test_revoke_and_disable_fail_closed(isolated_db, monkeypatch):
    from app import agents
    from app.store import migrate, open_db

    secret = _secret(monkeypatch)
    conn = open_db(isolated_db)
    migrate(conn)
    agents.create_agent(conn, secret, "temp")
    revoked = agents.revoke_agent_key(conn, "temp")
    assert revoked["revoked"] is True
    with pytest.raises(agents.AgentError) as exc:
        agents.resolve_signing_key(conn, secret, "temp")
    assert exc.value.code == "NO_ACTIVE_KEY"
    history = agents.key_history(conn, "temp")
    assert history[0]["status"] == "revoked"
    assert history[0]["revoked_at"] != ""

    agents.create_agent(conn, secret, "sleepy")
    agents.set_agent_status(conn, "sleepy", "disabled")
    with pytest.raises(agents.AgentError) as exc:
        agents.resolve_signing_key(conn, secret, "sleepy")
    assert exc.value.code == "NOT_ACTIVE"
    agents.set_agent_status(conn, "sleepy", "revoked")
    with pytest.raises(agents.AgentError) as exc:
        agents.resolve_signing_key(conn, secret, "sleepy")
    assert exc.value.code == "NOT_ACTIVE"
    with pytest.raises(agents.AgentError):
        agents.set_agent_status(conn, "sleepy", "banned")
    conn.close()


def test_agents_http_unauthenticated_rejected(isolated_db):
    from app.main import app

    bare = TestClient(app)
    assert bare.get("/agents").status_code == 401
    assert bare.post("/agents", json={"name": "x"}).status_code == 401
