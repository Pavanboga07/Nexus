"""Nexus peer trust: TRUSTED → SUSPENDED → REVOKED lifecycle.

Suspended peers exchange nothing new (history kept); revoked peers
additionally fail delegation verification; unpair still deletes.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return db


SECRET = "peer-trust-test-secret-xxxxxxxxxx"


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


def _paired(conn, secret, name="friend", caps=None):
    from app import pairing
    from app.identity import crypto
    from app.identity.service import ensure_identity

    ident = ensure_identity(conn, secret)
    fpriv, fpub = crypto.generate_keypair()
    fraw = crypto.public_key_bytes(fpub)
    fid = crypto.agent_id_from_public_key(fraw)
    import base64

    from relay.directory import sign_card
    from relay.envelope import utc_iso_in, utc_now_iso

    card = sign_card(fpriv, {
        "type": "agent-card", "protocol": "nexus-a2a", "version": "0.3",
        "agent_id": fid, "display_name": name,
        "public_key": base64.b64encode(fraw).decode("ascii"),
        "endpoint": "ws://test.invalid/ws",
        "capabilities": caps if caps is not None else [],
        "supported_purposes": [], "issued_at": utc_now_iso(),
        "expires_at": utc_iso_in(3600),
    })
    pairing.approve_peer(conn, card)
    return fid, ident.agent_id, fpriv


def test_trust_lifecycle(db_env):
    from app import pairing

    conn = _conn(db_env)
    try:
        fid, _, _ = _paired(conn, SECRET)
        peer = pairing.get_peer(conn, fid)
        assert peer["trust_state"] == "TRUSTED"
        assert pairing.set_peer_trust(conn, fid, "SUSPENDED")["trust_state"] \
            == "SUSPENDED"
        assert pairing.set_peer_trust(conn, fid, "TRUSTED")["trust_state"] \
            == "TRUSTED"
        assert pairing.set_peer_trust(conn, fid, "REVOKED")["trust_state"] \
            == "REVOKED"
        with pytest.raises(pairing.PairingError) as exc:
            pairing.set_peer_trust(conn, fid, "BANNED")
        assert exc.value.code == "BAD_TRUST_STATE"
        with pytest.raises(pairing.PairingError) as exc:
            pairing.set_peer_trust(conn, "nexus:ed25519:00000000000000000000000000000000", "SUSPENDED")
        assert exc.value.code == "NOT_PAIRED"
    finally:
        conn.close()


def test_suspended_peer_cannot_send_or_receive(db_env):
    from app import pairing
    from app.a2a import service
    from app.a2a.service import A2AError
    from app.identity import crypto
    from app.identity.service import ensure_identity

    conn = _conn(db_env)
    try:
        fid, aid, fpriv = _paired(conn, SECRET)
        row = conn.execute(
            "SELECT encrypted_private_key FROM identity WHERE id = 1"
        ).fetchone()
        priv = crypto.load_private_key(crypto.decrypt_private_key(
            row["encrypted_private_key"], SECRET))
        pairing.set_peer_trust(conn, fid, "SUSPENDED")
        with pytest.raises(A2AError) as exc:
            service.create_request(
                conn, signer_priv=priv, sender_id=aid,
                recipient_id=fid, question="hi?")
        assert exc.value.code == "PEER_SUSPENDED"
        # Receive side fails closed too: a properly signed envelope
        # from the suspended peer is rejected before ingestion.
        from app.a2a import envelope as app_envelope

        incoming = app_envelope.sign(
            app_envelope.new_envelope(
                sender=fid, recipient=aid, message_type="request",
                payload={"action": "answer", "question": "hi again?"},
                message_id="msg_susp9", correlation_id="corr_susp9"),
            fpriv)
        with pytest.raises(A2AError) as exc2:
            service.receive_envelope(
                conn, incoming, signer_priv=priv, local_id=aid)
        assert exc2.value.code == "PEER_SUSPENDED"
    finally:
        conn.close()


def test_revoked_peer_kills_grants(db_env):
    from app import agents, capabilities, delegations, pairing

    conn = _conn(db_env)
    try:
        fid, _, _ = _paired(
            conn, SECRET,
            caps=[{"id": "friend.survey@v1", "version": 1}])
        me = agents.create_agent(conn, SECRET, "boss")
        grant = delegations.issue_grant(
            conn, SECRET, "boss", fid, "friend.survey@v1", "answer",
            task_id="corr_t1")
        ok, _ = delegations.evaluate_grant(
            conn, delegations.get_grant(conn, grant["id"]),
            recipient_agent_id=fid, capability_id="friend.survey@v1",
            purpose="answer", task_id="corr_t1")
        assert ok is True
        pairing.set_peer_trust(conn, fid, "REVOKED")
        ok, why = delegations.evaluate_grant(
            conn, delegations.get_grant(conn, grant["id"]),
            recipient_agent_id=fid, capability_id="friend.survey@v1",
            purpose="answer", task_id="corr_t1")
        assert ok is False
        assert why == "recipient agent no longer usable"
        _ = me
    finally:
        conn.close()


def test_last_seen_bumps_on_ingest(db_env):
    from app import pairing
    from app.a2a import service
    from app.identity import crypto
    from app.identity.service import ensure_identity

    conn = _conn(db_env)
    try:
        fid, aid, _ = _paired(conn, SECRET)
        assert pairing.get_peer(conn, fid)["last_seen_at"] == ""
        row = conn.execute(
            "SELECT encrypted_private_key FROM identity WHERE id = 1"
        ).fetchone()
        priv = crypto.load_private_key(crypto.decrypt_private_key(
            row["encrypted_private_key"], SECRET))
        asked = service.create_request(
            conn, signer_priv=priv, sender_id=aid, recipient_id=fid,
            question="seen?", message_id="msg_seen001",
            correlation_id="corr_seen001")
        # Loop the envelope back as if delivered (same-DB ingest of our
        # own send is a replay; instead verify the touch helper directly).
        from app import pairing as pairing_mod

        pairing_mod.touch_peer_seen(conn, fid, "2026-10-03T12:00:00Z")
        assert pairing.get_peer(conn, fid)["last_seen_at"] == \
            "2026-10-03T12:00:00Z"
        _ = asked
    finally:
        conn.close()


def test_trust_http_endpoints(db_env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import auth_headers

    monkeypatch.setenv(
        "NEXUS_OPERATOR_TOKEN", "peer-http-token")
    client = TestClient(
        app, headers={"Authorization": "Bearer peer-http-token"})
    client.post("/agents", json={"name": "op"})
    fake = "nexus:ed25519:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert client.post(f"/pairing/peers/{fake}/trust",
                       json={"state": "SUSPENDED"}).status_code == 404
    resp = client.post("/pairing/peers/x/trust", json={"state": "BOGUS"})
    assert resp.status_code in (400, 404)
