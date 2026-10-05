"""V4 ask/approve/answer loop tests (TDD red first).

Covers the Task V4 contract: v0.3 envelope canonical rules (strict
timestamps, floats rejected, signature excluded), sign/verify, replayed
message_id rejected by DB UNIQUE constraint (never memory), expiry
enforcement, approval_request parking an inline-approvable card carrying
who/what/why/expiry, approve/reject resolution, unknown type -> signed
error, fail-closed policy (no rule -> ASK, sensitive -> DENY, ALLOW
rules user-created with specificity). Gate: scripted two-profile
exchange with approvals on both ends, every hop through the test relay
queue (extends the V3 two-profile pattern).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from tests.conftest import auth_headers

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-09-22T12:00:00Z"
GOOD_EXP = "2026-09-22T12:05:00Z"
# Far-future expiry for paths that compare against the real wall clock
# (relay queue claims, HTTP ingress): frozen NOW is hours behind it.
LIVE_EXP = "2027-09-22T12:05:00Z"


def make_profile(tmp_path, name):
    """Local profile: migrated SQLite + identity + live key handles."""
    from app.identity import crypto
    from app.identity.service import ensure_identity
    from app.store import migrate, open_db

    conn = open_db(str(tmp_path / f"{name}.db"))
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
    """Mutual approve so each side pins the other's key (V3 pattern)."""
    from app import pairing

    card_a = live_card(priv_a, ident_a.agent_id, pub_a, "Ada")
    card_b = live_card(priv_b, ident_b.agent_id, pub_b, "Blaise")
    pairing.approve_peer(conn_a, card_b, now=NOW)
    pairing.approve_peer(conn_b, card_a, now=NOW)


def ask(conn, signer_priv, sender_id, recipient_id, **kw):
    from app.a2a import service

    params = {
        "signer_priv": signer_priv,
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "question": "What is the capital?",
        "timestamp": NOW_ISO,
        "expires_at": GOOD_EXP,
    }
    params.update(kw)
    return service.create_request(conn, **params)


# --- canonical rules (via the app wrapper, same frozen v0.3) --------------

def test_app_envelope_rejects_non_strict_timestamp(tmp_path):
    from app.a2a import envelope as app_envelope

    with pytest.raises(Exception) as exc:
        app_envelope.new_envelope(
            sender="nexus:ed25519:10ba682c8ad13513971e8b56881aab8b",
            recipient="nexus:ed25519:1325b850c2871916eae203f0efc3c898",
            message_type="request",
            payload={"action": "answer"},
            timestamp="20-09-2026 12:00",
            expires_at=GOOD_EXP,
        )
    assert "timestamp" in str(exc.value).lower()


def test_app_envelope_rejects_float_payload(tmp_path):
    from app.a2a import envelope as app_envelope

    with pytest.raises(Exception) as exc:
        app_envelope.new_envelope(
            sender="nexus:ed25519:10ba682c8ad13513971e8b56881aab8b",
            recipient="nexus:ed25519:1325b850c2871916eae203f0efc3c898",
            message_type="request",
            payload={"score": 0.5},
            timestamp=NOW_ISO,
            expires_at=GOOD_EXP,
        )
    assert "float" in str(exc.value).lower()


def test_app_signature_excluded_from_signed_bytes(tmp_path):
    from app.a2a import envelope as app_envelope

    conn, ident, priv, pub_b64 = make_profile(tmp_path, "sig")
    unsigned = app_envelope.new_envelope(
        sender=ident.agent_id,
        recipient=ident.agent_id,
        message_type="request",
        payload={"action": "answer"},
        timestamp=NOW_ISO,
        expires_at=GOOD_EXP,
        message_id="msg_sig001",
        correlation_id="corr_sig001",
    )
    signed = app_envelope.sign(unsigned, priv)
    assert signed["signature"]
    assert "signature" not in app_envelope.unsigned_dict(signed)
    assert app_envelope.verify(signed, pub_b64) is True


def test_sign_verify_round_trip_and_tamper_fails(tmp_path):
    from app import pairing
    from app.a2a import envelope as app_envelope

    conn, ident, priv, pub_b64 = make_profile(tmp_path, "rt")
    pairing.approve_peer(
        conn, live_card(priv, ident.agent_id, pub_b64), now=NOW
    )
    signed = ask(
        conn, priv, ident.agent_id, ident.agent_id,
        message_id="msg_rt001", correlation_id="corr_rt001",
    )
    assert app_envelope.verify(signed, pub_b64) is True
    tampered = dict(signed)
    tampered["payload"] = {"action": "forged"}
    assert app_envelope.verify(tampered, pub_b64) is False

# --- replay, expiry -----------------------------------------------------

def test_replay_same_message_id_rejected_by_unique_constraint(tmp_path):
    from app.a2a import service
    from app.a2a.service import A2AError

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "ra")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "rb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_replay001", correlation_id="corr_replay001",
    )
    first = service.receive_envelope(
        conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    assert first["outcome"] == "parked"
    with pytest.raises(A2AError) as exc:
        service.receive_envelope(
            conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
        )
    assert exc.value.code == "REPLAY"
    # Constraint proof: the row exists once; rejection came from the DB.
    rows = conn_b.execute(
        "SELECT COUNT(*) AS n FROM a2a_messages WHERE message_id = ?",
        ("msg_replay001",),
    ).fetchone()
    assert rows["n"] == 1


def test_expired_envelope_rejected(tmp_path):
    from app.a2a import service
    from app.a2a.service import A2AError

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "ea")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "eb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_exp001", correlation_id="corr_exp001",
        timestamp="2026-09-22T12:00:00Z",
        expires_at="2026-09-22T12:01:00Z",
    )
    with pytest.raises(A2AError) as exc:
        service.receive_envelope(
            conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id,
            now="2026-09-22T12:02:00Z",
        )
    assert exc.value.code == "EXPIRED"


# --- parking + fail-closed policy ------------------------------------------

def test_approval_request_parks_card_with_evidence(tmp_path):
    from app.a2a import service

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "pa")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "pb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_type="approval_request",
        message_id="msg_park001", correlation_id="corr_park001",
        question="Share the budget summary?",
        data_category="general", purpose="summarize",
    )
    outcome = service.receive_envelope(
        conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    assert outcome["outcome"] == "parked"
    card = outcome["approval"]
    # Evidence block: who / what / why / expiry.
    assert card["requester"] == ident_a.agent_id
    assert "budget" in card["question"]
    assert card["purpose"] == "summarize"
    assert card["data_category"] == "general"
    assert card["expires_at"] == GOOD_EXP
    assert card["status"] == "pending"
    pending = service.list_approvals(conn_b)
    assert [c["approval_id"] for c in pending] == [card["approval_id"]]


def test_request_with_no_rule_parks_ask_fail_closed(tmp_path):
    from app.a2a import service

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "fa")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "fb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    # No policy rules at all: any disclosure defaults to ASK (parked).
    assert service.list_rules(conn_b) == []
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_ask001", correlation_id="corr_ask001",
    )
    outcome = service.receive_envelope(
        conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    assert outcome["outcome"] == "parked"
    assert outcome["approval"]["status"] == "pending"


def test_allow_rule_auto_allows(tmp_path):
    from app.a2a import service

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "aa")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "ab")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    service.create_rule(
        conn_b, peer=ident_a.agent_id, data_category="general",
        purpose="answer", action="answer",
    )
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_allow001", correlation_id="corr_allow001",
    )
    outcome = service.receive_envelope(
        conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    assert outcome["outcome"] == "auto_allow"
    assert service.list_approvals(conn_b) == []


def test_sensitive_category_denied_despite_allow_rule(tmp_path):
    from app.a2a import service

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "sa")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "sb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    service.create_rule(conn_b)  # wildcard ALLOW everything
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_sens001", correlation_id="corr_sens001",
        data_category="credentials", question="Send the API key?",
    )
    outcome = service.receive_envelope(
        conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    assert outcome["outcome"] == "denied"
    assert outcome["reply"]["message_type"] == "error"
    assert service.list_approvals(conn_b) == []


def test_policy_specificity_prefers_exact_match(tmp_path):
    from app.a2a import service

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "xa")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "xb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    # A wildcard rule for another category must not match...
    service.create_rule(conn_b, data_category="other")
    assert service.evaluate_policy(
        conn_b, peer=ident_a.agent_id, data_category="general",
        purpose="answer", action="answer",
    ) == "ASK"
    # ...while the exact rule allows.
    service.create_rule(
        conn_b, peer=ident_a.agent_id, data_category="general",
        purpose="answer", action="answer",
    )
    assert service.evaluate_policy(
        conn_b, peer=ident_a.agent_id, data_category="general",
        purpose="answer", action="answer",
    ) == "ALLOW"

# --- approve / reject / errors --------------------------------------------

def test_approve_resolves_with_signed_envelope(tmp_path):
    from app.a2a import envelope as app_envelope
    from app.a2a import service

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "va")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "vb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_apr001", correlation_id="corr_apr001",
    )
    parked = service.receive_envelope(
        conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    approval = service.approve_approval(
        conn_b, parked["approval"]["approval_id"],
        signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW,
    )
    assert approval["message_type"] == "approve"
    assert approval["correlation_id"] == "corr_apr001"
    assert app_envelope.verify(approval, pub_b) is True
    resolved = service.list_approvals(conn_b, status="approved")
    assert len(resolved) == 1
    assert service.list_approvals(conn_b) == []
    # Requester side: the approve resolves the exchange too.
    done = service.receive_envelope(
        conn_a, approval, signer_priv=priv_a, local_id=ident_a.agent_id,
        now=NOW,
    )
    assert done["outcome"] == "resolved"


def test_reject_resolves_with_signed_envelope(tmp_path):
    from app.a2a import envelope as app_envelope
    from app.a2a import service

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "ja")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "jb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_rej001", correlation_id="corr_rej001",
    )
    parked = service.receive_envelope(
        conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    rejection = service.reject_approval(
        conn_b, parked["approval"]["approval_id"],
        signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW,
    )
    assert rejection["message_type"] == "reject"
    assert app_envelope.verify(rejection, pub_b) is True
    assert service.list_approvals(conn_b, status="rejected")[0][
        "correlation_id"
    ] == "corr_rej001"
    done = service.receive_envelope(
        conn_a, rejection, signer_priv=priv_a, local_id=ident_a.agent_id,
        now=NOW,
    )
    assert done["outcome"] == "resolved"


def test_unknown_type_returns_signed_error(tmp_path):
    from app.a2a import envelope as app_envelope
    from app.a2a import service

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "ua")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "ub")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_unk001", correlation_id="corr_unk001",
    )
    env["message_type"] = "teleport"  # unknown, but still signed shape
    outcome = service.receive_envelope(
        conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    assert outcome["outcome"] == "error"
    err = outcome["reply"]
    assert err["message_type"] == "error"
    assert err["correlation_id"] == "corr_unk001"
    assert app_envelope.verify(err, pub_b) is True


def test_unpaired_sender_rejected_fail_closed(tmp_path):
    from app import pairing
    from app.a2a import service
    from app.a2a.service import A2AError

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "na")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "nb")
    # One-way pin: A may send, but B never pinned A, so B refuses.
    pairing.approve_peer(
        conn_a, live_card(priv_b, ident_b.agent_id, pub_b, "Blaise"), now=NOW
    )
    # Never paired: the edges trust pinned keys only.
    env = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_nopair001", correlation_id="corr_nopair001",
    )
    with pytest.raises(A2AError) as exc:
        service.receive_envelope(
            conn_b, env, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
        )
    assert exc.value.code == "NOT_PAIRED"


# --- gate: two-profile exchange, approvals both ends, via test relay --------

def _relay_hop(Session, envelope):
    """Push one envelope through the relay queue (persist + claim + ack).

    Reuses the test's fixture engine (the V2/V3 pattern): no extra
    engines, one ``run()`` per hop on the shared test loop.
    """
    from relay import queue
    from tests import relay_db

    async def main():
        async with Session() as s:
            status = await queue.enqueue(s, envelope=dict(envelope))
            await s.commit()
        async with Session() as s:
            rows = await queue.claim_for_recipient(
                s, envelope["recipient"], limit=10
            )
            await s.commit()
        assert status in ("queued", "dedup_hit")
        assert [r["message_id"] for r in rows] == [envelope["message_id"]]
        async with Session() as s:
            assert await queue.ack_delivered(s, envelope["message_id"]) is True
            await s.commit()

    relay_db.run(main())


def test_two_profile_exchange_approvals_both_ends_via_relay(
    relay_engine, tmp_path
):
    """Gate: A asks B (B approves, B answers), then B asks A mirrored."""
    from app.a2a import envelope as app_envelope
    from app.a2a import service
    from relay.db import make_session_factory

    Session = make_session_factory(relay_engine)

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "gatea")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "gateb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )

    # Leg 1: A -> B request, B parks + approves, B -> A response.
    req = ask(
        conn_a, priv_a, ident_a.agent_id, ident_b.agent_id,
        message_id="msg_gate001", correlation_id="corr_gate001",
        question="Summarize the notes?", expires_at=LIVE_EXP,
    )
    _relay_hop(Session, req)
    parked = service.receive_envelope(
        conn_b, req, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )
    assert parked["outcome"] == "parked"
    # Decided in real time: this envelope crosses the relay, whose
    # queue enforces expiry against the wall clock (frozen NOW would
    # arrive already expired).
    approval = service.approve_approval(
        conn_b, parked["approval"]["approval_id"],
        signer_priv=priv_b, local_id=ident_b.agent_id,
    )
    _relay_hop(Session, approval)
    # Validated in real time to match the envelope's real stamp.
    seen = service.receive_envelope(
        conn_a, approval, signer_priv=priv_a, local_id=ident_a.agent_id,
    )
    assert seen["outcome"] == "resolved"
    resp = service.send_response(
        conn_b, signer_priv=priv_b, sender_id=ident_b.agent_id,
        recipient_id=ident_a.agent_id, correlation_id="corr_gate001",
        answer="The notes say hello.", timestamp=NOW_ISO,
        expires_at=LIVE_EXP, message_id="msg_gate002",
    )
    _relay_hop(Session, resp)
    answered = service.receive_envelope(
        conn_a, resp, signer_priv=priv_a, local_id=ident_a.agent_id, now=NOW
    )
    assert answered["outcome"] == "answered"
    assert app_envelope.verify(resp, pub_b) is True

    # Leg 2 (mirrored): B -> A request, A parks + approves, A -> B answer.
    req2 = ask(
        conn_b, priv_b, ident_b.agent_id, ident_a.agent_id,
        message_id="msg_gate003", correlation_id="corr_gate002",
        question="What did we decide?", expires_at=LIVE_EXP,
    )
    _relay_hop(Session, req2)
    parked2 = service.receive_envelope(
        conn_a, req2, signer_priv=priv_a, local_id=ident_a.agent_id, now=NOW
    )
    assert parked2["outcome"] == "parked"
    # Real time again: this envelope crosses the real-clock relay.
    approval2 = service.approve_approval(
        conn_a, parked2["approval"]["approval_id"],
        signer_priv=priv_a, local_id=ident_a.agent_id,
    )
    _relay_hop(Session, approval2)
    assert service.receive_envelope(
        conn_b, approval2, signer_priv=priv_b, local_id=ident_b.agent_id,
    )["outcome"] == "resolved"
    resp2 = service.send_response(
        conn_a, signer_priv=priv_a, sender_id=ident_a.agent_id,
        recipient_id=ident_b.agent_id, correlation_id="corr_gate002",
        answer="We decided to ship.", timestamp=NOW_ISO,
        expires_at=LIVE_EXP, message_id="msg_gate004",
    )
    _relay_hop(Session, resp2)
    assert service.receive_envelope(
        conn_b, resp2, signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW
    )["outcome"] == "answered"

# --- local API routes (thin glue; no relay needed) --------------------------

def _ask_client(tmp_path, monkeypatch, name="r"):
    from app.identity.service import ensure_identity
    from app.main import app
    from app.store import migrate, open_db

    db_path = str(tmp_path / f"{name}.db")
    secret = f"route-secret-{name}-xxxxxxxxxxxx"
    conn = open_db(db_path)
    migrate(conn)
    ensure_identity(conn, secret)
    conn.close()
    monkeypatch.setenv("NEXUS_DB_PATH", db_path)
    monkeypatch.setenv("NEXUS_IDENTITY_KEY", secret)
    monkeypatch.delenv("NEXUS_RELAY_URL", raising=False)
    return TestClient(app, headers=auth_headers())


def _route_peer(client, display_name="Blaise"):
    """Pin a fresh peer straight into the route DB (no relay needed)."""
    from tests.relay_db import new_agent

    priv, pub_b64, agent_id = new_agent()
    card = live_card(priv, agent_id, pub_b64, display_name)
    resp = client.post("/pairing/approve", json={"card": card})
    assert resp.status_code == 200, resp.text
    return agent_id


def test_ask_route_sends_request_to_paired_peer(tmp_path, monkeypatch):
    client = _ask_client(tmp_path, monkeypatch)
    peer_id = _route_peer(client)
    resp = client.post(
        "/ask",
        json={"peer_agent_id": peer_id, "question": "Summarize?"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recipient"] == peer_id
    assert body["correlation_id"]
    assert body["envelope"]["message_type"] == "request"


def test_ask_route_rejects_unknown_peer(tmp_path, monkeypatch):
    client = _ask_client(tmp_path, monkeypatch)
    resp = client.post(
        "/ask",
        json={
            "peer_agent_id": "nexus:ed25519:ffffffffffffffffffffffffffffffff",
            "question": "Hi?",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_PAIRED"


def test_ask_routes_approvals_flow(tmp_path, monkeypatch):
    """Full card loop over HTTP: incoming parks, approve resolves."""
    import os

    from app.a2a import envelope as app_envelope
    from app.identity.service import load_identity
    from app.store import open_db
    from tests.relay_db import new_agent

    client = _ask_client(tmp_path, monkeypatch, name="flow")
    peer_priv, peer_pub, peer_id = new_agent()
    resp = client.post(
        "/pairing/approve",
        json={"card": live_card(peer_priv, peer_id, peer_pub, "Blaise")},
    )
    assert resp.status_code == 200, resp.text
    conn = open_db(os.environ["NEXUS_DB_PATH"])
    try:
        local_id = load_identity(
            conn, os.environ["NEXUS_IDENTITY_KEY"]
        ).agent_id
    finally:
        conn.close()

    env = app_envelope.sign(
        app_envelope.new_envelope(
            sender=peer_id,
            recipient=local_id,
            message_type="request",
            payload={
                "action": "answer",
                "question": "Summarize the notes?",
                "data_category": "general",
                "purpose": "answer",
            },
            timestamp=NOW_ISO,
            expires_at=LIVE_EXP,
            message_id="msg_flow001",
            correlation_id="corr_flow001",
        ),
        peer_priv,
    )
    incoming = client.post("/ask/incoming", json={"envelope": env})
    assert incoming.status_code == 200, incoming.text
    assert incoming.json()["outcome"] == "parked"

    pending = client.get("/ask/approvals").json()["approvals"]
    assert len(pending) == 1
    card = pending[0]
    assert card["requester"] == peer_id  # who
    assert "notes" in card["question"]  # what
    assert card["purpose"] == "answer"  # why
    assert card["expires_at"] == LIVE_EXP  # expiry

    approved = client.post(
        f"/ask/approvals/{card['approval_id']}/approve"
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["envelope"]["message_type"] == "approve"
    assert client.get("/ask/approvals").json() == {"approvals": []}

    # Second ask -> reject with a reason (exercises the optional body).
    env2 = app_envelope.sign(
        app_envelope.new_envelope(
            sender=peer_id,
            recipient=local_id,
            message_type="request",
            payload={
                "action": "answer",
                "question": "Share the vault?",
                "data_category": "general",
                "purpose": "answer",
            },
            timestamp=NOW_ISO,
            expires_at=LIVE_EXP,
            message_id="msg_flow002",
            correlation_id="corr_flow002",
        ),
        peer_priv,
    )
    assert client.post("/ask/incoming", json={"envelope": env2}).json()[
        "outcome"
    ] == "parked"
    second_id = client.get("/ask/approvals").json()["approvals"][0][
        "approval_id"
    ]
    denied = client.post(
        f"/ask/approvals/{second_id}/reject", json={"reason": "no time"}
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["envelope"]["message_type"] == "reject"
    assert denied.json()["envelope"]["payload"]["reason"] == "no time"


def test_ask_route_incoming_unknown_type_signed_error(tmp_path, monkeypatch):
    """Unknown message_type over HTTP ingress -> signed error (200)."""
    import base64
    import os

    from app.identity import crypto
    from app.identity.service import load_identity
    from app.store import open_db
    from relay.envelope import canonical_json_bytes
    from tests.relay_db import new_agent

    client = _ask_client(tmp_path, monkeypatch, name="unk")
    peer_priv, peer_pub, peer_id = new_agent()
    resp = client.post(
        "/pairing/approve",
        json={"card": live_card(peer_priv, peer_id, peer_pub, "Blaise")},
    )
    assert resp.status_code == 200, resp.text
    conn = open_db(os.environ["NEXUS_DB_PATH"])
    try:
        local_id = load_identity(
            conn, os.environ["NEXUS_IDENTITY_KEY"]
        ).agent_id
    finally:
        conn.close()

    # Hand-sign an unknown-type envelope (bypasses schema-validated
    # helpers, exactly like a hostile sender would).
    unsigned = {
        "protocol": "nexus-a2a",
        "version": "0.3",
        "message_id": "msg_unk001",
        "correlation_id": "corr_unk001",
        "sender": peer_id,
        "recipient": local_id,
        "timestamp": NOW_ISO,
        "expires_at": GOOD_EXP,
        "message_type": "teleport",
        "payload": {"action": "answer"},
    }
    raw_sig = crypto.sign_bytes(peer_priv, canonical_json_bytes(unsigned))
    env = {
        **unsigned,
        "signature": base64.b64encode(raw_sig).decode("ascii"),
    }
    incoming = client.post("/ask/incoming", json={"envelope": env})
    assert incoming.status_code == 200, incoming.text
    body = incoming.json()
    assert body["outcome"] == "error"
    assert body["reply"]["message_type"] == "error"
    assert body["reply"]["correlation_id"] == "corr_unk001"


def test_ask_policy_routes_crud(tmp_path, monkeypatch):
    client = _ask_client(tmp_path, monkeypatch, name="pol")
    assert client.get("/ask/policy").json() == {"rules": []}
    created = client.post(
        "/ask/policy", json={"data_category": "general"}
    )
    assert created.status_code == 200, created.text
    rule_id = created.json()["rule_id"]
    rules = client.get("/ask/policy").json()["rules"]
    assert [r["rule_id"] for r in rules] == [rule_id]
    deleted = client.delete(f"/ask/policy/{rule_id}")
    assert deleted.json() == {"rule_id": rule_id, "removed": True}
    assert client.get("/ask/policy").json() == {"rules": []}
