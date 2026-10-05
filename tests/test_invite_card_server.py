"""Item 1: server-built invite cards (TDD).

``POST /pairing/invites`` must build the 12-field card server-side from
the local key instead of requiring a client-supplied card. The primary
path sends NO card; NOT_LOCAL_CARD stays only as defense when a
mismatched card IS supplied.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import auth_headers


def _seed_local_profile(tmp_path, monkeypatch, name="inviter"):
    from app.identity.service import ensure_identity
    from app.store import migrate, open_db

    db_path = str(tmp_path / f"{name}.db")
    secret = f"invite-secret-{name}-xxxxxxxxxxxx"
    conn = open_db(db_path)
    migrate(conn)
    ident = ensure_identity(conn, secret)
    conn.close()
    monkeypatch.setenv("NEXUS_DB_PATH", db_path)
    monkeypatch.setenv("NEXUS_IDENTITY_KEY", secret)
    return ident


def _relay_publish_transport(relay_client):
    """Publish transport backed by a relay TestClient (no ports)."""
    from app.pairing import PairingError

    def publish(code, card):
        resp = relay_client.post("/invites", json={"code": code, "card": card})
        body = resp.json()
        if resp.status_code != 200:
            raise PairingError(
                body.get("code", "UNKNOWN"),
                body.get("detail", "publish failed"),
                status=resp.status_code,
            )
        return body

    return publish


def test_invite_succeeds_with_no_card_in_body(relay_engine, tmp_path, monkeypatch):
    from app import pairing
    from app.main import app
    from relay.main import create_relay_app
    from tests.relay_db import TEST_URL

    ident = _seed_local_profile(tmp_path, monkeypatch)
    # Transport is mocked to the relay TestClient; the URL is unused.
    monkeypatch.setenv("NEXUS_RELAY_URL", "http://relay.test")
    with TestClient(create_relay_app(database_url=TEST_URL)) as relay:
        monkeypatch.setattr(
            pairing, "http_publish_transport",
            lambda base_url: _relay_publish_transport(relay),
        )
        client = TestClient(app, headers=auth_headers())
        # Primary path: NO card in the body at all.
        resp = client.post("/pairing/invites", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["code"].split("-")) == 6

        # The published card verifies LOCALLY and pins the local key.
        claimed = relay.post(
            "/invites/claim", json={"code": body["code"]}
        )
        assert claimed.status_code == 200, claimed.text
        card = claimed.json()["card"]
        agent_id, fingerprint = pairing.verify_card_and_fingerprint(card)
        assert agent_id == ident.agent_id
        assert fingerprint == ident.fingerprint


def test_invite_no_card_fingerprint_equality_both_sides(
    relay_engine, tmp_path, monkeypatch
):
    from app import pairing
    from app.identity.service import load_identity
    from app.main import app
    from app.store import open_db
    from relay.main import create_relay_app
    from tests.relay_db import TEST_URL

    _seed_local_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("NEXUS_RELAY_URL", "http://relay.test")
    with TestClient(create_relay_app(database_url=TEST_URL)) as relay:
        monkeypatch.setattr(
            pairing, "http_publish_transport",
            lambda base_url: _relay_publish_transport(relay),
        )
        client = TestClient(app, headers=auth_headers())
        resp = client.post(
            "/pairing/invites", json={"display_name": "Ada"}
        )
        assert resp.status_code == 200, resp.text

        claimed = relay.post(
            "/invites/claim", json={"code": resp.json()["code"]}
        )
        card = claimed.json()["card"]
        # Claimant side: fingerprint from the verified card.
        _, claimant_fp = pairing.verify_card_and_fingerprint(card)
        # Inviter side: same fingerprint from its own stored identity.
        conn = open_db(f"{tmp_path}/inviter.db")
        try:
            local = load_identity(
                conn, "invite-secret-inviter-xxxxxxxxxxxx"
            )
        finally:
            conn.close()
        assert claimant_fp == local.fingerprint
        assert card["display_name"] == "Ada"


def test_invite_mismatched_card_still_403(tmp_path, monkeypatch):
    from app.main import app
    from tests.relay_db import new_agent
    from relay.directory import sign_card
    from relay.envelope import utc_iso_in, utc_now_iso

    _seed_local_profile(tmp_path, monkeypatch)
    monkeypatch.delenv("NEXUS_RELAY_URL", raising=False)
    priv, pub_b64, agent_id = new_agent()
    foreign = sign_card(
        priv,
        {
            "type": "agent-card",
            "protocol": "nexus-a2a",
            "version": "0.3",
            "agent_id": agent_id,
            "display_name": "Mallory",
            "public_key": pub_b64,
            "endpoint": "ws://test.invalid/ws",
            "capabilities": [],
            "supported_purposes": [],
            "issued_at": utc_now_iso(),
            "expires_at": utc_iso_in(365 * 24 * 3600),
        },
    )
    client = TestClient(app, headers=auth_headers())
    resp = client.post("/pairing/invites", json={"card": foreign})
    assert resp.status_code == 403
    assert resp.json()["code"] == "NOT_LOCAL_CARD"


def test_invite_empty_card_object_is_rejected(tmp_path, monkeypatch):
    """The old frontend payload (``card: {}``) must not build anything."""
    from app.main import app

    _seed_local_profile(tmp_path, monkeypatch)
    monkeypatch.delenv("NEXUS_RELAY_URL", raising=False)
    client = TestClient(app, headers=auth_headers())
    resp = client.post("/pairing/invites", json={"card": {}})
    assert resp.status_code == 403
    assert resp.json()["code"] == "NOT_LOCAL_CARD"


def test_build_local_card_has_twelve_signed_fields(tmp_path):
    from app import pairing
    from app.identity.service import ensure_identity
    from app.store import migrate, open_db

    conn = open_db(str(tmp_path / "card.db"))
    migrate(conn)
    ident = ensure_identity(conn, "card-secret-xxxxxxxxxxxx")
    card = pairing.build_local_card(conn, "card-secret-xxxxxxxxxxxx")
    assert set(card) == {
        "type", "protocol", "version", "agent_id", "display_name",
        "public_key", "endpoint", "capabilities", "supported_purposes",
        "issued_at", "expires_at", "signature",
    }
    agent_id, fingerprint = pairing.verify_card_and_fingerprint(card)
    assert agent_id == ident.agent_id
    assert fingerprint == ident.fingerprint
    conn.close()
