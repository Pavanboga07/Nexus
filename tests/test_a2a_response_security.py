"""A2A response-path security: responses must be as strict as requests.

Covers timestamp window (expired + future), signature binding,
replay, and — the previously missing check — that a response
continues a conversation WE started with THIS peer (correlation +
direction), so paired strangers cannot inject answers.
"""

from __future__ import annotations

import datetime

import pytest

from app.a2a.service import A2AError
from tests.test_a2a_loop import make_profile, pair_both_ways


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_plus(seconds: int) -> str:
    return _iso(
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=seconds)
    )


@pytest.fixture
def pair(tmp_path):
    from app import pairing

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "a")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "b")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    return {
        "conn_a": conn_a,
        "conn_b": conn_b,
        "id_a": ident_a.agent_id,
        "id_b": ident_b.agent_id,
        "priv_a": priv_a,
        "priv_b": priv_b,
    }


def _request(conn, priv, sender, recipient, question="q?", **kw):
    from app.a2a import service

    return service.create_request(
        conn,
        signer_priv=priv,
        sender_id=sender,
        recipient_id=recipient,
        question=question,
        data_category="general",
        purpose="answer",
        **kw,
    )


def _receive(conn, envelope, priv, local_id):
    from app.a2a import service

    return service.receive_envelope(
        conn, envelope, signer_priv=priv, local_id=local_id
    )


def test_future_timestamp_rejected(pair):
    from app.a2a import envelope as app_envelope

    unsigned = app_envelope.new_envelope(
        sender=pair["id_a"],
        recipient=pair["id_b"],
        message_type="request",
        payload={"action": "answer", "question": "from the future"},
        timestamp=_now_plus(3600),
        expires_at=_now_plus(7200),
    )
    signed = app_envelope.sign(unsigned, pair["priv_a"])
    with pytest.raises(A2AError) as exc:
        _receive(pair["conn_b"], signed, pair["priv_b"], pair["id_b"])
    assert exc.value.code == "FUTURE_TIMESTAMP"


def test_expired_envelope_rejected(pair):
    from app.a2a import envelope as app_envelope

    unsigned = app_envelope.new_envelope(
        sender=pair["id_a"],
        recipient=pair["id_b"],
        message_type="request",
        payload={"action": "answer", "question": "too late"},
        timestamp=_now_plus(-7200),
        expires_at=_now_plus(-3600),
    )
    signed = app_envelope.sign(unsigned, pair["priv_a"])
    with pytest.raises(A2AError) as exc:
        _receive(pair["conn_b"], signed, pair["priv_b"], pair["id_b"])
    assert exc.value.code == "EXPIRED"


def test_uncorrelated_response_rejected(pair):
    from app.a2a import service

    # B answers a conversation that never happened.
    forged = service.send_response(
        pair["conn_b"],
        signer_priv=pair["priv_b"],
        sender_id=pair["id_b"],
        recipient_id=pair["id_a"],
        correlation_id="corr_invented_123",
        answer="trust me",
    )
    with pytest.raises(A2AError) as exc:
        _receive(pair["conn_a"], forged, pair["priv_a"], pair["id_a"])
    assert exc.value.code == "UNCORRELATED"


def test_valid_request_response_round_trip(pair):
    from app.a2a import service

    asked = _request(
        pair["conn_a"], pair["priv_a"], pair["id_a"], pair["id_b"],
        question="what is up?",
    )
    parked = _receive(
        pair["conn_b"], asked, pair["priv_b"], pair["id_b"]
    )
    assert parked["outcome"] == "parked"
    corr = asked["correlation_id"]
    answered = service.send_response(
        pair["conn_b"],
        signer_priv=pair["priv_b"],
        sender_id=pair["id_b"],
        recipient_id=pair["id_a"],
        correlation_id=corr,
        answer="the sky",
    )
    done = _receive(
        pair["conn_a"], answered, pair["priv_a"], pair["id_a"]
    )
    assert done["outcome"] == "answered"


def test_replayed_response_rejected(pair):
    from app.a2a import service

    asked = _request(
        pair["conn_a"], pair["priv_a"], pair["id_a"], pair["id_b"]
    )
    _receive(pair["conn_b"], asked, pair["priv_b"], pair["id_b"])
    answered = service.send_response(
        pair["conn_b"],
        signer_priv=pair["priv_b"],
        sender_id=pair["id_b"],
        recipient_id=pair["id_a"],
        correlation_id=asked["correlation_id"],
        answer="once",
    )
    assert (
        _receive(pair["conn_a"], answered, pair["priv_a"], pair["id_a"])[
            "outcome"
        ]
        == "answered"
    )
    with pytest.raises(A2AError) as exc:
        _receive(pair["conn_a"], answered, pair["priv_a"], pair["id_a"])
    assert exc.value.code == "REPLAY"


def test_approve_from_non_requester_rejected(tmp_path):
    """A third paired peer naming our correlation ID cannot resolve
    our cards — approvals only complete with the peer we asked."""
    from app.a2a import envelope as app_envelope
    from app.a2a import service
    from app.a2a.service import A2AError
    from tests.test_a2a_loop import make_profile, pair_both_ways

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "a")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "b")
    conn_c, ident_c, priv_c, pub_c = make_profile(tmp_path, "c")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_c, ident_c, priv_c, pub_c
    )
    asked = service.create_request(
        conn_a, signer_priv=priv_a, sender_id=ident_a.agent_id,
        recipient_id=ident_b.agent_id, question="q?",
        data_category="general", purpose="answer",
    )
    forged = app_envelope.sign(
        app_envelope.new_envelope(
            sender=ident_c.agent_id, recipient=ident_a.agent_id,
            message_type="approve",
            payload={"approved": True, "scope": "answer-once"},
            correlation_id=asked["correlation_id"],
        ),
        priv_c,
    )
    with pytest.raises(A2AError) as exc:
        service.receive_envelope(
            conn_a, forged, signer_priv=priv_a, local_id=ident_a.agent_id
        )
    assert exc.value.code == "UNCORRELATED"


def test_tampered_signature_rejected(pair):
    asked = _request(
        pair["conn_a"], pair["priv_a"], pair["id_a"], pair["id_b"]
    )
    tampered = dict(asked)
    tampered["payload"] = dict(asked["payload"], question="forged text")
    with pytest.raises(A2AError) as exc:
        _receive(pair["conn_b"], tampered, pair["priv_b"], pair["id_b"])
    assert exc.value.code == "INVALID_SIGNATURE"
