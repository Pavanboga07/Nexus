"""V3 pairing tests (TDD red first).

Invite codes: 6 words from an embedded 2048-word list (11 bits/word =
66 bits), single-use, 15-min expiry. Wrong code -> INVALID_CODE +
attempt counted (5 tries -> COOLDOWN); expired -> EXPIRED with a
regenerate hint; second claim -> ALREADY_CLAIMED. Fingerprints come
from the locally VERIFIED card on both sides, never relay metadata.
Unpair is local-only (works with the peer unreachable). Gate: two
local profiles pair through the relay test instance in both
directions (mutual approve -> pinned keys).
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from tests.conftest import auth_headers

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


def make_profile(tmp_path, name):
    from app.identity.service import ensure_identity
    from app.store import migrate, open_db

    conn = open_db(str(tmp_path / f"{name}.db"))
    migrate(conn)
    ident = ensure_identity(conn, f"test-secret-{name}-xxxxxxxxxxxx")
    return conn, ident


def profile_keypair(conn, secret):
    from app.identity import crypto

    row = conn.execute(
        "SELECT public_key, encrypted_private_key FROM identity WHERE id = 1"
    ).fetchone()
    public_raw = base64.b64decode(row["public_key"])
    private_raw = crypto.decrypt_private_key(
        row["encrypted_private_key"], secret
    )
    return (
        crypto.load_private_key(private_raw),
        public_raw,
        base64.b64encode(public_raw).decode("ascii"),
    )


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


def make_relay_app():
    from relay.main import create_relay_app
    from tests.relay_db import TEST_URL

    return create_relay_app(database_url=TEST_URL)


def make_testclient_transport(client):
    """Claim/publish transports backed by a relay TestClient (no ports)."""
    from app.pairing import PairingError

    def claim_fn(code):
        resp = client.post("/invites/claim", json={"code": code})
        body = resp.json()
        if resp.status_code == 200:
            return body["card"]
        raise PairingError(
            body.get("code", "UNKNOWN"),
            body.get("detail", "claim failed"),
            status=resp.status_code,
        )

    def publish_fn(code, card):
        resp = client.post("/invites", json={"code": code, "card": card})
        body = resp.json()
        if resp.status_code != 200:
            raise PairingError(
                body.get("code", "UNKNOWN"),
                body.get("detail", "publish failed"),
                status=resp.status_code,
            )
        return body

    return claim_fn, publish_fn


# --- code generation ----------------------------------------------------

def test_wordlist_has_2048_entries_for_66_bit_codes():
    from app import pairing

    assert len(pairing.WORDLIST) == 2048
    assert len(set(pairing.WORDLIST)) == 2048
    assert pairing.CODE_WORDS == 6
    assert pairing.CODE_WORDS * 11 == 66


def test_generated_code_is_six_words_from_wordlist():
    from app import pairing

    code = pairing.generate_code()
    words = code.split("-")
    assert len(words) == pairing.CODE_WORDS
    assert all(w in pairing.WORDLIST for w in words)


def test_generated_codes_differ():
    from app import pairing

    assert len({pairing.generate_code() for _ in range(50)}) == 50


def test_code_normalization_accepts_spaces_and_case():
    from app import pairing

    code = pairing.generate_code()
    spaced = "  " + code.replace("-", "  ").upper() + " "
    assert pairing.normalize_code(spaced) == code
    assert pairing.code_hash(spaced) == pairing.code_hash(code)
    assert len(pairing.code_hash(code)) == 64


def test_code_normalization_rejects_bad_shape():
    from app import pairing

    with pytest.raises(pairing.PairingError) as exc:
        pairing.normalize_code("too-few-words")
    assert exc.value.code == "INVALID_CODE"

# --- invite lifecycle over the relay --------------------------------------

def test_relay_invite_create_and_claim_happy_path(relay_engine, tmp_path):
    from app import pairing

    conn_a, ident_a = make_profile(tmp_path, "a")
    priv_a, _, pub_a = profile_keypair(conn_a, "test-secret-a-xxxxxxxxxxxx")
    card_a = live_card(priv_a, ident_a.agent_id, pub_a)

    with TestClient(make_relay_app()) as client:
        claim_fn, publish_fn = make_testclient_transport(client)
        invite = pairing.create_invite(
            conn_a, agent_id=ident_a.agent_id, card=card_a,
            now=NOW, publish_fn=publish_fn,
        )
        assert len(invite["code"].split("-")) == 6

        trust = pairing.claim_invite(
            conn_a, invite["code"], now=NOW, claim_fn=claim_fn,
        )
        # Trust screen carries verified-card fields only, no relay metadata.
        assert set(trust) == {"agent_id", "display_name", "fingerprint", "card"}
        assert trust["agent_id"] == ident_a.agent_id


def test_claim_twice_is_already_claimed(relay_engine, tmp_path):
    from app import pairing

    conn_a, ident_a = make_profile(tmp_path, "a")
    priv_a, _, pub_a = profile_keypair(conn_a, "test-secret-a-xxxxxxxxxxxx")
    card_a = live_card(priv_a, ident_a.agent_id, pub_a)

    with TestClient(make_relay_app()) as client:
        claim_fn, publish_fn = make_testclient_transport(client)
        invite = pairing.create_invite(
            conn_a, agent_id=ident_a.agent_id, card=card_a,
            now=NOW, publish_fn=publish_fn,
        )
        pairing.claim_invite(conn_a, invite["code"], now=NOW, claim_fn=claim_fn)
        with pytest.raises(pairing.PairingError) as exc:
            pairing.claim_invite(
                conn_a, invite["code"], now=NOW, claim_fn=claim_fn
            )
        assert exc.value.code == "ALREADY_CLAIMED"
        assert exc.value.status == 409
        assert "already claimed" in str(exc.value).lower()


def test_wrong_code_counts_attempts_then_cooldown(relay_engine, tmp_path):
    from app import pairing

    conn_b, _ = make_profile(tmp_path, "b")
    with TestClient(make_relay_app()) as client:
        claim_fn, _ = make_testclient_transport(client)
        codes = [pairing.generate_code() for _ in range(6)]
        for code in codes[:5]:
            with pytest.raises(pairing.PairingError) as exc:
                pairing.claim_invite(
                    conn_b, code, now=NOW, claim_fn=claim_fn
                )
            assert exc.value.code == "INVALID_CODE"
            assert exc.value.status == 404
        with pytest.raises(pairing.PairingError) as exc:
            pairing.claim_invite(
                conn_b, codes[5], now=NOW, claim_fn=claim_fn
            )
        assert exc.value.code == "COOLDOWN"
        assert exc.value.status == 429


def test_expired_invite_points_to_regenerate(relay_engine, tmp_path):
    from app import pairing

    conn_a, ident_a = make_profile(tmp_path, "a2")
    priv_a, _, pub_a = profile_keypair(conn_a, "test-secret-a2-xxxxxxxxxxxx")
    card_a = live_card(priv_a, ident_a.agent_id, pub_a)
    code = pairing.generate_code()

    with TestClient(make_relay_app()) as client:
        claim_fn, _ = make_testclient_transport(client)
        resp = client.post(
            "/invites", json={"code": code, "card": card_a, "ttl_seconds": 0}
        )
        assert resp.status_code == 200
        with pytest.raises(pairing.PairingError) as exc:
            pairing.claim_invite(conn_a, code, now=NOW, claim_fn=claim_fn)
        assert exc.value.code == "EXPIRED"
        assert exc.value.status == 410
        assert "new code" in str(exc.value).lower()


def test_relay_rejects_forged_card_on_publish(relay_engine, tmp_path):
    from app import pairing

    conn_a, ident_a = make_profile(tmp_path, "a3")
    _, _, pub_a = profile_keypair(conn_a, "test-secret-a3-xxxxxxxxxxxx")
    from app.identity import crypto

    other_priv, _ = crypto.generate_keypair()
    forged = live_card(other_priv, ident_a.agent_id, pub_a)
    code = pairing.generate_code()

    with TestClient(make_relay_app()) as client:
        resp = client.post("/invites", json={"code": code, "card": forged})
        assert resp.status_code == 401

# --- fingerprints, pinning, unpair, gate ----------------------------------

def test_fingerprint_comes_from_verified_card_both_sides(
    relay_engine, tmp_path
):
    from app import pairing
    from app.identity import crypto

    conn_a, ident_a = make_profile(tmp_path, "ga")
    priv_a, raw_a, pub_a = profile_keypair(
        conn_a, "test-secret-ga-xxxxxxxxxxxx"
    )
    card_a = live_card(priv_a, ident_a.agent_id, pub_a)

    with TestClient(make_relay_app()) as client:
        claim_fn, publish_fn = make_testclient_transport(client)
        invite = pairing.create_invite(
            conn_a, agent_id=ident_a.agent_id, card=card_a,
            now=NOW, publish_fn=publish_fn,
        )
        trust = pairing.claim_invite(
            conn_a, invite["code"], now=NOW, claim_fn=claim_fn,
        )
        expected = crypto.fingerprint_from_public_key(raw_a)
        # Claimant side: fingerprint of the verified card's key.
        assert trust["fingerprint"] == expected
        # Inviter side: same fingerprint from its own verified card.
        _, inviter_fp = pairing.verify_card_and_fingerprint(card_a)
        assert inviter_fp == expected
        assert trust["fingerprint"] == ident_a.fingerprint


def test_forged_card_fails_local_verification(tmp_path):
    from app import pairing
    from app.identity import crypto

    conn_a, ident_a = make_profile(tmp_path, "fa")
    _, _, pub_a = profile_keypair(conn_a, "test-secret-fa-xxxxxxxxxxxx")
    other_priv, _ = crypto.generate_keypair()
    forged = live_card(other_priv, ident_a.agent_id, pub_a)
    with pytest.raises(pairing.PairingError) as exc:
        pairing.verify_card_and_fingerprint(forged)
    assert exc.value.code in ("INVALID_SIGNATURE", "IDENTITY_MISMATCH")
    with pytest.raises(pairing.PairingError):
        pairing.approve_peer(conn_a, forged, now=NOW)
    assert pairing.list_peers(conn_a) == []


def test_mutual_approve_pins_keys_both_ways(relay_engine, tmp_path):
    from app import pairing

    conn_a, ident_a = make_profile(tmp_path, "ma")
    conn_b, ident_b = make_profile(tmp_path, "mb")
    priv_a, _, pub_a = profile_keypair(conn_a, "test-secret-ma-xxxxxxxxxxxx")
    priv_b, _, pub_b = profile_keypair(conn_b, "test-secret-mb-xxxxxxxxxxxx")
    card_a = live_card(priv_a, ident_a.agent_id, pub_a, "Ada")
    card_b = live_card(priv_b, ident_b.agent_id, pub_b, "Blaise")

    with TestClient(make_relay_app()) as client:
        claim_fn, publish_fn = make_testclient_transport(client)
        # A invites; B claims and pins A.
        invite_a = pairing.create_invite(
            conn_a, agent_id=ident_a.agent_id, card=card_a,
            now=NOW, publish_fn=publish_fn,
        )
        trust_b = pairing.claim_invite(
            conn_b, invite_a["code"], now=NOW, claim_fn=claim_fn,
        )
        peer_on_b = pairing.approve_peer(conn_b, trust_b["card"], now=NOW)
        assert peer_on_b["agent_id"] == ident_a.agent_id
        assert peer_on_b["fingerprint"] == ident_a.fingerprint
        # B invites; A claims and pins B (mutual approve).
        invite_b = pairing.create_invite(
            conn_b, agent_id=ident_b.agent_id, card=card_b,
            now=NOW, publish_fn=publish_fn,
        )
        trust_a = pairing.claim_invite(
            conn_a, invite_b["code"], now=NOW, claim_fn=claim_fn,
        )
        peer_on_a = pairing.approve_peer(conn_a, trust_a["card"], now=NOW)
        assert peer_on_a["agent_id"] == ident_b.agent_id
        assert peer_on_a["fingerprint"] == ident_b.fingerprint
        # Pins persist on both profiles.
        assert pairing.get_peer(conn_a, ident_b.agent_id)["fingerprint"] == (
            ident_b.fingerprint
        )
        assert pairing.get_peer(conn_b, ident_a.agent_id)["fingerprint"] == (
            ident_a.fingerprint
        )


def test_unpair_removes_locally_with_peer_unreachable(tmp_path):
    from app import pairing

    conn_a, ident_a = make_profile(tmp_path, "ua")
    conn_b, ident_b = make_profile(tmp_path, "ub")
    priv_b, _, pub_b = profile_keypair(conn_b, "test-secret-ub-xxxxxxxxxxxx")
    card_b = live_card(priv_b, ident_b.agent_id, pub_b, "Blaise")
    pairing.approve_peer(conn_a, card_b, now=NOW)
    assert pairing.get_peer(conn_a, ident_b.agent_id) is not None

    def unreachable_claim(code):
        raise pairing.PairingError("UNREACHABLE", "relay is down")

    # Unpair is local-only: no network call, works while relay is down.
    assert pairing.unpair(conn_a, ident_b.agent_id) is True
    assert pairing.get_peer(conn_a, ident_b.agent_id) is None
    assert pairing.list_peers(conn_a) == []
    assert pairing.unpair(conn_a, ident_b.agent_id) is False
    assert ident_a.agent_id != ident_b.agent_id


def test_local_store_migrates_pairing_tables(tmp_path):
    from app.store import get_version, migrate, open_db

    conn = open_db(str(tmp_path / "mig.db"))
    assert migrate(conn) >= 2
    assert get_version(conn) >= 2
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"invites", "pair_attempts", "paired_peers"} <= tables
    conn.close()


# --- local API routes (thin glue; no relay needed) --------------------------

def _route_client(tmp_path, monkeypatch):
    from app.main import app

    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "routes.db"))
    monkeypatch.delenv("NEXUS_RELAY_URL", raising=False)
    return TestClient(app, headers=auth_headers())


def test_routes_peers_empty_and_unpair_unknown(tmp_path, monkeypatch):
    client = _route_client(tmp_path, monkeypatch)
    assert client.get("/pairing/peers").json() == {"peers": []}
    resp = client.delete(
        "/pairing/peers/nexus:ed25519:00000000000000000000000000000000"
    )
    assert resp.status_code == 200
    assert resp.json()["removed"] is False


def test_routes_claim_malformed_code_without_relay(tmp_path, monkeypatch):
    client = _route_client(tmp_path, monkeypatch)
    resp = client.post("/pairing/claim", json={"code": "not a code"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_CODE"


def test_routes_approve_garbage_card_rejected(tmp_path, monkeypatch):
    client = _route_client(tmp_path, monkeypatch)
    resp = client.post("/pairing/approve", json={"card": {"agent_id": "x"}})
    assert resp.status_code in (400, 401)


def test_concurrent_claim_single_winner(relay_engine, tmp_path):
    """Race: two simultaneous claims on one code -> exactly one wins.

    Real concurrency (asyncio.gather, separate sessions against the test
    PG DB, no mocks). Single-use code: loser must see ALREADY_CLAIMED.
    """
    import asyncio

    from sqlalchemy import text
    from relay import invites
    from relay.db import make_session_factory
    from tests import relay_db

    from app import pairing

    conn_a, ident_a = make_profile(tmp_path, "race")
    priv_a, _, pub_a = profile_keypair(conn_a, "test-secret-race-xxxxxxxxxxxx")
    card_a = live_card(priv_a, ident_a.agent_id, pub_a)
    code = pairing.generate_code()

    async def main():
        Session = make_session_factory(relay_engine)
        async with Session() as s:
            await invites.create_invite_entry(s, card_a, code)
            await s.commit()

        # Pre-open both sessions so each holds a live pooled connection:
        # without this the first claim can fully commit before the second
        # connection finishes its handshake, hiding the race.
        s1, s2 = Session(), Session()
        try:
            await s1.execute(text("SELECT 1"))
            await s2.execute(text("SELECT 1"))

            async def attempt(session, ip):
                try:
                    card = await invites.claim_invite_entry(session, code, ip)
                    await session.commit()
                    return ("ok", card["agent_id"])
                except invites.InviteClaimError as exc:
                    return ("err", exc.code, exc.status)

            return await asyncio.gather(
                attempt(s1, "10.9.0.1"), attempt(s2, "10.9.0.2")
            )
        finally:
            await s1.close()
            await s2.close()

    res_a, res_b = relay_db.run(main())
    wins = [r for r in (res_a, res_b) if r[0] == "ok"]
    losses = [r for r in (res_a, res_b) if r[0] == "err"]
    assert len(wins) == 1, f"expected exactly one winner, got: {res_a!r} {res_b!r}"
    assert wins[0][1] == ident_a.agent_id
    assert len(losses) == 1
    assert losses[0][1] == "ALREADY_CLAIMED"
    assert losses[0][2] == 409

