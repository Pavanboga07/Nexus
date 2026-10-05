"""Operator authentication edge: bearer token on every non-open path.

Covers the human/operator trust domain (separate from agent Ed25519
crypto): open health, fail-closed 401s with WWW-Authenticate, wrong
token rejected, env-pinned token accepted, rotation kills the old
token, and the principal shape itself.
"""

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


def test_health_is_open(isolated_db):
    from app.main import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_unauthenticated_api_fails_closed(isolated_db):
    from app.main import app

    client = TestClient(app)
    for path in (
        "/chat/threads",
        "/memory",
        "/pairing/peers",
        "/pairing/me",
        "/backup",
        "/settings/llm-status",
        "/ask/approvals",
    ):
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["code"] == "UNAUTHORIZED"
    assert (
        client.get("/chat/threads").headers.get("www-authenticate") == "Bearer"
    )


def test_wrong_token_rejected(isolated_db):
    from app.main import app

    client = TestClient(app)
    resp = client.get(
        "/chat/threads", headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401
    resp = client.get("/chat/threads", headers={"Authorization": "Token nope"})
    assert resp.status_code == 401


def test_env_pinned_token_accepted(isolated_db, monkeypatch):
    from app.main import app

    monkeypatch.setenv("NEXUS_OPERATOR_TOKEN", "op-secret")
    client = TestClient(app)
    resp = client.get(
        "/chat/threads", headers={"Authorization": "Bearer op-secret"}
    )
    assert resp.status_code == 200


def test_rotation_kills_old_token(isolated_db):
    from app import machine_config
    from app.main import app

    old, created = machine_config.get_or_create_operator_token()
    assert created is True
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {old}"}
    assert client.get("/chat/threads", headers=headers).status_code == 200
    resp = client.post("/settings/operator-token/rotate", headers=headers)
    assert resp.status_code == 200
    new = resp.json()["token"]
    assert new and new != old
    assert client.get("/chat/threads", headers=headers).status_code == 401
    assert (
        client.get(
            "/chat/threads", headers={"Authorization": f"Bearer {new}"}
        ).status_code
        == 200
    )


def test_rotation_refused_when_pinned(isolated_db, monkeypatch):
    from app.main import app

    monkeypatch.setenv("NEXUS_OPERATOR_TOKEN", "op-secret")
    client = TestClient(app)
    resp = client.post(
        "/settings/operator-token/rotate",
        headers={"Authorization": "Bearer op-secret"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "PINNED"


def test_operator_token_file_created_once(isolated_db):
    from app import machine_config

    first, created_first = machine_config.get_or_create_operator_token()
    assert created_first is True
    assert machine_config.operator_token_path().exists()
    second, created_second = machine_config.get_or_create_operator_token()
    assert (second, created_second) == (first, False)
    assert machine_config.verify_operator_token(first) is True
    assert machine_config.verify_operator_token("wrong") is False
    assert machine_config.verify_operator_token("") is False


def test_cors_origins_env_driven(isolated_db, monkeypatch):
    from app import main

    assert "http://localhost:3000" in main._cors_origins()
    monkeypatch.setenv(
        "NEXUS_CORS_ORIGINS", "https://app.example.com, http://localhost:9999"
    )
    assert main._cors_origins() == [
        "https://app.example.com",
        "http://localhost:9999",
    ]


def test_request_id_minted_and_echoed(isolated_db):
    from app.main import app

    client = TestClient(app)
    first = client.get("/health")
    assert first.headers.get("x-request-id", "").startswith("req_")
    second = client.get("/health")
    assert second.headers["x-request-id"] != first.headers["x-request-id"]
    pinned = client.get(
        "/health", headers={"X-Request-ID": "req_custom123"}
    )
    assert pinned.headers["x-request-id"] == "req_custom123"


def test_principal_shape():
    from app.auth import AuthenticatedPrincipal

    principal = AuthenticatedPrincipal(owner_id="local", agent_id="default")
    assert principal.has_scope("operator") is True
    assert principal.has_scope("admin") is False


def test_live_ticket_single_use_and_expiry(isolated_db):
    from app import auth

    ticket = auth.issue_live_ticket()
    assert ticket
    assert auth.redeem_live_ticket(ticket) is True
    assert auth.redeem_live_ticket(ticket) is False  # consumed
    assert auth.redeem_live_ticket("nope") is False
    assert auth.redeem_live_ticket("") is False
    stale = auth.issue_live_ticket()
    auth._live_tickets[stale] = 0.0  # force expiry (test-only clock tweak)
    assert auth.redeem_live_ticket(stale) is False


def test_live_ticket_endpoint_requires_auth(isolated_db):
    from app.main import app

    client = TestClient(app)
    assert client.post("/ask/live-ticket").status_code == 401


def test_live_bridge_ticket_handshake(isolated_db, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from app import auth
    from app.main import app

    monkeypatch.setenv("NEXUS_OPERATOR_TOKEN", "op-ticket-secret")
    # Hermetic: no relay regardless of ambient machine env, so the
    # route deterministically closes 1011 after the ticket passes.
    monkeypatch.delenv("NEXUS_RELAY_URL", raising=False)
    client = TestClient(
        app, headers={"Authorization": "Bearer op-ticket-secret"}
    )
    ticket = client.post("/ask/live-ticket").json()["ticket"]
    assert ticket
    # Ticketed handshake completes; the route then closes 1011 because no
    # relay is configured in tests.
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ask/live?ticket={ticket}") as ws:
            ws.receive_text()
    assert exc.value.code == 1011
    # Bare handshake (no ticket) is closed 4401 in-route: the auth
    # middleware never sees websocket scopes (Starlette limitation), so
    # the bridge enforces the ticket itself.
    from fastapi.testclient import TestClient as BareClient

    bare = BareClient(app)
    with pytest.raises(WebSocketDisconnect) as denied:
        with bare.websocket_connect("/ask/live") as ws:
            ws.receive_text()
    assert denied.value.code == 4401
    assert auth.redeem_live_ticket(ticket) is False  # consumed by handshake
