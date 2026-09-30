"""Approval expiry enforcement (fix #2 TDD red first)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-09-22T12:00:00Z"
GOOD_EXP = "2026-09-22T12:05:00Z"
PAST_EXP = "2026-09-22T11:00:00Z"


def make_profile(tmp_path, name):
    import base64

    from app.identity import crypto
    from app.identity.service import ensure_identity
    from app.store import migrate, open_db

    conn = open_db(str(tmp_path / f"exp_{name}.db"))
    migrate(conn)
    secret = f"test-secret-{name}-xxxxxxxxxxxx"
    ident = ensure_identity(conn, secret)
    row = conn.execute(
        "SELECT public_key, encrypted_private_key FROM identity WHERE id = 1"
    ).fetchone()
    public_raw = base64.b64decode(row["public_key"])
    private_raw = crypto.decrypt_private_key(
        row["encrypted_private_key"], secret
    )
    priv = crypto.load_private_key(private_raw)
    pub_b64 = base64.b64encode(public_raw).decode("ascii")
    return conn, ident, priv, pub_b64


def live_card(priv, agent_id, pub_b64, display_name="Ada"):
    from relay.directory import sign_card
    from relay.envelope import utc_iso_in, utc_now_iso

    return sign_card(
        priv,
        {
            "type": "agent-card",
            "protocol": "nexus-a2a",
            "version": "0.3",
            "agent_id": agent_id,
            "display_name": display_name,
            "public_key": pub_b64,
            "endpoint": "ws://test.invalid/ws",
            "capabilities": [],
            "supported_purposes": [],
            "issued_at": utc_now_iso(),
            "expires_at": utc_iso_in(365 * 24 * 3600),
        },
    )


def pair_both_ways(conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b):
    from app import pairing

    pairing.approve_peer(
        conn_a, live_card(priv_b, ident_b.agent_id, pub_b, "Blaise"), now=NOW
    )
    pairing.approve_peer(
        conn_b, live_card(priv_a, ident_a.agent_id, pub_a, "Ada"), now=NOW
    )


def park_card(tmp_path, tag, monkeypatch=None):
    from app.a2a import service

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, f"{tag}a")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, f"{tag}b")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    env = service.create_request(
        conn_a,
        signer_priv=priv_a,
        sender_id=ident_a.agent_id,
        recipient_id=ident_b.agent_id,
        question="Share the notes?",
        timestamp=NOW_ISO,
        expires_at=GOOD_EXP,
        message_id=f"msg_{tag}001",
        correlation_id=f"corr_{tag}001",
    )
    outcome = service.receive_envelope(
        conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    assert outcome["outcome"] == "parked"
    return conn_b, ident_b, priv_b, outcome["approval"]


def test_approve_past_expiry_pending_returns_410(tmp_path):
    from app.a2a import service
    from app.a2a.service import A2AError

    conn_b, ident_b, priv_b, card = park_card(tmp_path, "past")
    with conn_b:
        conn_b.execute(
            "UPDATE a2a_approvals SET expires_at = ? WHERE approval_id = ?",
            (PAST_EXP, card["approval_id"]),
        )
    with pytest.raises(A2AError) as exc:
        service.approve_approval(
            conn_b, card["approval_id"],
            signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW,
        )
    assert exc.value.code == "EXPIRED"
    assert exc.value.status == 410


def test_reject_past_expiry_pending_returns_410(tmp_path):
    from app.a2a import service
    from app.a2a.service import A2AError

    conn_b, ident_b, priv_b, card = park_card(tmp_path, "pastr")
    with conn_b:
        conn_b.execute(
            "UPDATE a2a_approvals SET expires_at = ? WHERE approval_id = ?",
            (PAST_EXP, card["approval_id"]),
        )
    with pytest.raises(A2AError) as exc:
        service.reject_approval(
            conn_b, card["approval_id"],
            signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW,
        )
    assert exc.value.code == "EXPIRED"
    assert exc.value.status == 410


def test_decided_expired_returns_409_precedence(tmp_path):
    from app.a2a import service
    from app.a2a.service import A2AError

    conn_b, ident_b, priv_b, card = park_card(tmp_path, "dec")
    service.approve_approval(
        conn_b, card["approval_id"],
        signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW,
    )
    with conn_b:
        conn_b.execute(
            "UPDATE a2a_approvals SET expires_at = ? WHERE approval_id = ?",
            (PAST_EXP, card["approval_id"]),
        )
    with pytest.raises(A2AError) as exc:
        service.approve_approval(
            conn_b, card["approval_id"],
            signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW,
        )
    assert exc.value.code == "ALREADY_DECIDED"
    assert exc.value.status == 409


def test_malformed_expiry_never_raw_envelope_error(tmp_path):
    from app.a2a import service
    from app.a2a.service import A2AError
    from relay.envelope import EnvelopeError

    conn_b, ident_b, priv_b, card = park_card(tmp_path, "mal")
    with conn_b:
        conn_b.execute(
            "UPDATE a2a_approvals SET expires_at = ? WHERE approval_id = ?",
            ("not-a-timestamp", card["approval_id"]),
        )
    with pytest.raises(A2AError) as exc:
        service.approve_approval(
            conn_b, card["approval_id"],
            signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW,
        )
    assert not isinstance(exc.value, EnvelopeError)
    assert isinstance(exc.value, A2AError)
    assert exc.value.status == 410


def test_missing_expiry_never_raw_type_error(tmp_path):
    from app.a2a import service
    from app.a2a.service import A2AError

    conn_b, ident_b, priv_b, card = park_card(tmp_path, "miss")
    with conn_b:
        conn_b.execute(
            "UPDATE a2a_approvals SET expires_at = ? WHERE approval_id = ?",
            ("", card["approval_id"]),
        )
    with pytest.raises(A2AError) as exc:
        service.approve_approval(
            conn_b, card["approval_id"],
            signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW,
        )
    assert isinstance(exc.value, A2AError)
    assert exc.value.status == 400
