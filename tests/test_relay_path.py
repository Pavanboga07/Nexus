"""Item 2: relay path + response route (TDD).

- ``POST /ask/response`` calls the existing (uncalled) ``send_response``.
- Relay WebSocket speaks the exact ``relay/main.py`` frame protocol
  (auth_challenge/auth_response handshake, 4401 on failure,
  relay_envelope send, delivery_ack).
- ``POST /ask`` queues background delivery (queued immediately, relayed
  / delivery_failed observable on refresh + retry; local-only only when
  no relay is configured, never silent); scripted two-profile ask->approve->answer runs over the test
  relay instance (extends the V4 gate pattern).
- ``/ask/live`` bridges relay deliveries to the chat browser (ingest +
  ack + forward); auth failure closes 4401.
"""

from __future__ import annotations

import asyncio
import base64
import threading

import pytest
from fastapi.testclient import TestClient

LIVE_EXP = "2027-09-22T12:05:00Z"
NOW_ISO = "2026-09-22T12:00:00Z"


def make_profile(tmp_path, name):
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
    from datetime import datetime, timezone

    from app import pairing

    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    pairing.approve_peer(
        conn_a, live_card(priv_b, ident_b.agent_id, pub_b, "Blaise"), now=now
    )
    pairing.approve_peer(
        conn_b, live_card(priv_a, ident_a.agent_id, pub_a, "Ada"), now=now
    )


# --- relay_client frame protocol (against the real test relay) --------------

class AsyncWs:
    """Drive a sync TestClient socket from async helpers under test.

    Blocking portal calls hop to a worker thread so the test loop never
    stalls; the relay app keeps running on its own portal thread.
    """

    def __init__(self, raw):
        self._raw = raw

    async def send_json(self, frame):
        return await asyncio.to_thread(self._raw.send_json, frame)

    async def receive_json(self):
        return await asyncio.to_thread(self._raw.receive_json)

    async def close(self):
        return await asyncio.to_thread(self._raw.close)


async def _authed(ws, priv, pub_b64, agent_id):
    from app.a2a import relay_client as rc
    from app.identity import crypto

    return await rc.authenticate(
        ws, agent_id=agent_id, public_key_b64=pub_b64,
        sign_fn=lambda data: crypto.sign_bytes(priv, data),
    )


def test_client_handshake_ok(relay_engine, tmp_path):
    from relay.main import create_relay_app
    from tests import relay_db
    from tests.relay_db import TEST_URL

    _, ident, priv, pub_b64 = make_profile(tmp_path, "hs")
    with TestClient(
        create_relay_app(database_url=TEST_URL, ack_timeout=1.0)
    ) as client:
        with client.websocket_connect("/ws") as raw:
            assert relay_db.run(
                _authed(AsyncWs(raw), priv, pub_b64, ident.agent_id)
            ) == ident.agent_id


def test_client_handshake_bad_signature_raises_4401(relay_engine, tmp_path):
    from app.a2a import relay_client as rc
    from app.identity import crypto
    from relay.main import create_relay_app
    from tests import relay_db
    from tests.relay_db import TEST_URL, new_agent

    _, _, victim_priv, victim_pub, victim_id = (None, None, *new_agent())
    _, other_pub, _ = new_agent()
    with TestClient(
        create_relay_app(database_url=TEST_URL, ack_timeout=1.0)
    ) as client:
        with client.websocket_connect("/ws") as raw:
            ws = AsyncWs(raw)

            async def _bad_auth():
                return await rc.authenticate(
                    ws,
                    agent_id=victim_id,
                    public_key_b64=other_pub,  # wrong binding: bad sig
                    sign_fn=lambda data: crypto.sign_bytes(victim_priv, data),
                )

            with pytest.raises(rc.RelayAuthError) as exc:
                relay_db.run(_bad_auth())
            assert "4401" in str(exc.value) or "4401" in str(
                getattr(exc.value, "code", "")
            )


def test_client_send_envelope_gets_delivery_ack(relay_engine, tmp_path):
    from app.a2a import relay_client as rc
    from tests import relay_db
    from tests.relay_db import TEST_URL, make_envelope
    from relay.main import create_relay_app

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "se1")
    _, ident_b, priv_b, pub_b = make_profile(tmp_path, "se2")

    with TestClient(
        create_relay_app(database_url=TEST_URL, ack_timeout=5.0)
    ) as client:
        with client.websocket_connect("/ws") as raw_b:
            ws_b = AsyncWs(raw_b)
            relay_db.run(_authed(ws_b, priv_b, pub_b, ident_b.agent_id))
            with client.websocket_connect("/ws") as raw_a:
                ws_a = AsyncWs(raw_a)
                relay_db.run(_authed(ws_a, priv_a, pub_a, ident_a.agent_id))
                env = make_envelope(
                    sender=ident_a.agent_id, recipient=ident_b.agent_id,
                    message_id="msg_client001",
                    correlation_id="corr_client001",
                    expires_at=LIVE_EXP,
                )

                # Live "delivered" needs B's ack while A waits: run the
                # sender on a thread, ack on this thread (portal serves
                # both concurrently; a sequential test would only ever
                # see the ack-timeout "queued").
                box: dict = {}

                def _go():
                    box["status"] = asyncio.run(
                        rc.send_envelope(
                            ws_a, envelope=env,
                            recipient=ident_b.agent_id,
                            relay_id="relay_client001",
                        )
                    )

                sender = threading.Thread(target=_go, daemon=True)
                sender.start()
                delivery = relay_db.run(ws_b.receive_json())
                assert delivery["type"] == "delivery"
                assert delivery["envelope"]["message_id"] == "msg_client001"
                relay_db.run(rc.ack_delivery(ws_b, delivery["relay_id"]))
                sender.join(timeout=30)
                assert box.get("status") == "delivered"


# --- stdlib connector against an ephemeral loopback WS server ----------------

def test_stdlib_connector_round_trips_over_ephemeral_port():
    """open_connection speaks RFC6455 with zero new dependencies."""
    import socket
    import threading

    from app.a2a import relay_client as rc
    from tests import relay_db

    received: list = []

    def _server(sock, ready):
        import base64 as _b64
        import hashlib as _hl
        import re as _re

        conn, _ = sock.accept()
        conn.settimeout(10)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        head = data.decode("latin1")
        key = _re.search(r"Sec-WebSocket-Key:\s*(\S+)", head).group(1)
        accept = _b64.b64encode(
            _hl.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        conn.sendall(
            ("HTTP/1.1 101 Switching Protocols\r\n"
             "Upgrade: websocket\r\n"
             "Connection: Upgrade\r\n"
             f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
        )
        ready.set()
        # Read one masked client text frame, echo it back unmasked.
        import struct as _st

        def _read_exact(n):
            buf = b""
            while len(buf) < n:
                chunk = conn.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("closed")
                buf += chunk
            return buf

        b1, b2 = _read_exact(2)
        ln = b2 & 0x7F
        if ln == 126:
            (ln,) = _st.unpack("!H", _read_exact(2))
        elif ln == 127:
            (ln,) = _st.unpack("!Q", _read_exact(8))
        mask = _read_exact(4)
        payload = _read_exact(ln)
        raw = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        received.append(raw)
        conn.sendall(bytes([0x81, len(raw)]) + raw)
        conn.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    ready = threading.Event()
    thread = threading.Thread(
        target=_server, args=(sock, ready), daemon=True
    )
    thread.start()

    async def _main():
        ws = await rc.open_connection(f"ws://127.0.0.1:{port}/ws")
        try:
            await ws.send_json({"type": "ping", "n": 1})
            return await ws.receive_json()
        finally:
            await ws.close()

    reply = relay_db.run(_main())
    thread.join(timeout=10)
    sock.close()
    assert reply == {"type": "ping", "n": 1}


# --- gate: two profiles ask->approve->answer OVER relay sockets --------------

def test_two_profile_ask_approve_answer_over_relay_sockets(
    relay_engine, tmp_path
):
    """Gate: every hop travels as relay_envelope/delivery over WS."""
    from app.a2a import relay_client as rc
    from app.a2a import service
    from relay.main import create_relay_app
    from tests import relay_db
    from tests.relay_db import TEST_URL

    conn_a, ident_a, priv_a, pub_a = make_profile(tmp_path, "gatea")
    conn_b, ident_b, priv_b, pub_b = make_profile(tmp_path, "gateb")
    pair_both_ways(
        conn_a, ident_a, priv_a, pub_a, conn_b, ident_b, priv_b, pub_b
    )

    async def _send(ws, env, recipient, relay_id):
        return await rc.send_envelope(
            ws, envelope=env, recipient=recipient, relay_id=relay_id
        )

    def _deliver_and_ack(ws_from, env, recipient, relay_id, ws_to):
        """Send on a thread while the recipient acks here (live path).

        Sequential code would only ever see the ack-timeout "queued";
        true "delivered" needs both sides moving at once.
        """
        box: dict = {}

        def _go():
            box["status"] = asyncio.run(
                _send(ws_from, env, recipient, relay_id)
            )

        sender = threading.Thread(target=_go, daemon=True)
        sender.start()
        frame = relay_db.run(ws_to.receive_json())
        assert frame["type"] == "delivery"
        relay_db.run(rc.ack_delivery(ws_to, frame["relay_id"]))
        sender.join(timeout=30)
        assert box.get("status") == "delivered", box
        return frame["envelope"]

    with TestClient(
        create_relay_app(database_url=TEST_URL, ack_timeout=5.0)
    ) as client:
        with client.websocket_connect("/ws") as raw_b:
            ws_b = AsyncWs(raw_b)
            relay_db.run(_authed(ws_b, priv_b, pub_b, ident_b.agent_id))
            with client.websocket_connect("/ws") as raw_a:
                ws_a = AsyncWs(raw_a)
                relay_db.run(_authed(ws_a, priv_a, pub_a, ident_a.agent_id))

                # A asks B over the socket.
                req = service.create_request(
                    conn_a, signer_priv=priv_a, sender_id=ident_a.agent_id,
                    recipient_id=ident_b.agent_id,
                    question="Summarize the notes?",
                    timestamp=NOW_ISO, expires_at=LIVE_EXP,
                    message_id="msg_relay001",
                    correlation_id="corr_relay001",
                )
                got = _deliver_and_ack(
                    ws_a, req, ident_b.agent_id, "relay_ask001", ws_b
                )
                parked = service.receive_envelope(
                    conn_b, got, signer_priv=priv_b,
                    local_id=ident_b.agent_id, now=NOW_ISO,
                )
                assert parked["outcome"] == "parked"

                # B approves; the approve travels back over the socket.
                approval = service.approve_approval(
                    conn_b, parked["approval"]["approval_id"],
                    signer_priv=priv_b, local_id=ident_b.agent_id, now=NOW_ISO,
                )
                seen = _deliver_and_ack(
                    ws_b, approval, ident_a.agent_id, "relay_apr001", ws_a
                )
                assert service.receive_envelope(
                    conn_a, seen, signer_priv=priv_a,
                    local_id=ident_a.agent_id, now=NOW_ISO,
                )["outcome"] == "resolved"

                # B answers; the response travels over the socket.
                resp = service.send_response(
                    conn_b, signer_priv=priv_b, sender_id=ident_b.agent_id,
                    recipient_id=ident_a.agent_id,
                    correlation_id="corr_relay001",
                    answer="The notes say hello.",
                    timestamp=NOW_ISO, expires_at=LIVE_EXP,
                    message_id="msg_relay002",
                )
                final = _deliver_and_ack(
                    ws_b, resp, ident_a.agent_id, "relay_resp001", ws_a
                )
                assert service.receive_envelope(
                    conn_a, final, signer_priv=priv_a,
                    local_id=ident_a.agent_id, now=NOW_ISO,
                )["outcome"] == "answered"


# --- HTTP routes --------------------------------------------------------------

def _ask_client(tmp_path, monkeypatch, name="r2", relay_url=None):
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
    if relay_url is None:
        monkeypatch.delenv("NEXUS_RELAY_URL", raising=False)
    else:
        monkeypatch.setenv("NEXUS_RELAY_URL", relay_url)
    return TestClient(app)


def _route_peer(client, display_name="Blaise"):
    from tests.relay_db import new_agent

    priv, pub_b64, agent_id = new_agent()
    card = live_card(priv, agent_id, pub_b64, display_name)
    resp = client.post("/pairing/approve", json={"card": card})
    assert resp.status_code == 200, resp.text
    return agent_id


def test_response_route_sends_answer(tmp_path, monkeypatch):
    client = _ask_client(tmp_path, monkeypatch)
    peer_id = _route_peer(client)
    resp = client.post(
        "/ask/response",
        json={
            "peer_agent_id": peer_id,
            "correlation_id": "corr_http001",
            "answer": "The notes say hello.",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["envelope"]["message_type"] == "response"
    assert body["envelope"]["payload"]["answer"] == "The notes say hello."
    assert body["correlation_id"] == "corr_http001"
    assert body["delivery"]["mode"] == "local-only"


def test_response_route_rejects_unknown_peer(tmp_path, monkeypatch):
    client = _ask_client(tmp_path, monkeypatch)
    resp = client.post(
        "/ask/response",
        json={
            "peer_agent_id": "nexus:ed25519:ffffffffffffffffffffffffffffffff",
            "correlation_id": "corr_http002",
            "answer": "hi",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_PAIRED"


def test_ask_route_reports_queued_when_relay_unreachable(
    tmp_path, monkeypatch
):
    """sendAsk never stalls and never lies: unreachable relay -> the
    request returns queued immediately; the background worker flips the
    stored row to delivery_failed (visible on refresh)."""
    client = _ask_client(
        tmp_path, monkeypatch, relay_url="http://127.0.0.1:1"
    )
    peer_id = _route_peer(client)
    resp = client.post(
        "/ask", json={"peer_agent_id": peer_id, "question": "Summarize?"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["envelope"]["message_type"] == "request"
    assert body["delivery"]["mode"] == "queued"
    assert "background" in body["delivery"].get("reason", "").lower()
    rows = client.get("/ask/messages").json()["messages"]
    assert [r["message_id"] for r in rows] == [body["message_id"]]
    assert rows[0]["status"] == "delivery_failed"


def test_ask_route_reports_relayed_when_delivered(tmp_path, monkeypatch):
    import app.api.routes.ask as ask_route
    from app.a2a import relay_client as rc

    async def _fake_deliver_one(*args, **kwargs):
        return "delivered"

    monkeypatch.setattr(rc, "deliver_one", _fake_deliver_one)
    client = _ask_client(
        tmp_path, monkeypatch, relay_url="http://relay.test"
    )
    peer_id = _route_peer(client)
    resp = client.post(
        "/ask", json={"peer_agent_id": peer_id, "question": "Summarize?"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["delivery"] == {
        "mode": "queued",
        "reason": ask_route.QUEUED_DELIVERY["reason"],
    }
    rows = client.get("/ask/messages").json()["messages"]
    assert rows[0]["status"] == "relayed"


def test_decide_routes_report_delivery(tmp_path, monkeypatch):
    """Approve/reject envelopes relay too (loop breaks remotely without)."""
    import os

    from app.a2a import envelope as app_envelope
    from app.identity.service import load_identity
    from app.store import open_db
    from tests.relay_db import new_agent

    client = _ask_client(
        tmp_path, monkeypatch, name="dec", relay_url="http://127.0.0.1:1"
    )
    peer_priv, peer_pub, peer_id = new_agent()
    assert client.post(
        "/pairing/approve",
        json={"card": live_card(peer_priv, peer_id, peer_pub, "Blaise")},
    ).status_code == 200
    conn = open_db(os.environ["NEXUS_DB_PATH"])
    try:
        local_id = load_identity(
            conn, os.environ["NEXUS_IDENTITY_KEY"]
        ).agent_id
    finally:
        conn.close()
    env = app_envelope.sign(
        app_envelope.new_envelope(
            sender=peer_id, recipient=local_id, message_type="request",
            payload={"action": "answer", "question": "Summarize?",
                     "data_category": "general", "purpose": "answer"},
            timestamp=NOW_ISO, expires_at=LIVE_EXP,
            message_id="msg_dec001", correlation_id="corr_dec001",
        ),
        peer_priv,
    )
    assert client.post("/ask/incoming", json={"envelope": env}).json()[
        "outcome"
    ] == "parked"
    approval_id = client.get("/ask/approvals").json()["approvals"][0][
        "approval_id"
    ]
    approved = client.post(f"/ask/approvals/{approval_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["delivery"]["mode"] == "queued"


# --- /ask/live bridge ----------------------------------------------------------

def _live_client(tmp_path, monkeypatch, name="live"):
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
    monkeypatch.setenv("NEXUS_RELAY_URL", "http://relay.test")
    return TestClient(app)


def test_live_bridge_forwards_delivery_and_acks(tmp_path, monkeypatch):
    import json

    import app.api.routes.ask as ask_route
    from app.a2a import envelope as app_envelope
    from app.identity.service import load_identity
    from app.store import open_db
    from tests.relay_db import new_agent

    client = _live_client(tmp_path, monkeypatch)
    peer_priv, peer_pub, peer_id = new_agent()
    assert client.post(
        "/pairing/approve",
        json={"card": live_card(peer_priv, peer_id, peer_pub, "Blaise")},
    ).status_code == 200
    conn = open_db(__import__("os").environ["NEXUS_DB_PATH"])
    try:
        local_id = load_identity(
            conn, __import__("os").environ["NEXUS_IDENTITY_KEY"]
        ).agent_id
    finally:
        conn.close()
    env = app_envelope.sign(
        app_envelope.new_envelope(
            sender=peer_id, recipient=local_id, message_type="request",
            payload={"action": "answer", "question": "Relayed hello?",
                     "data_category": "general", "purpose": "answer"},
            timestamp=NOW_ISO, expires_at=LIVE_EXP,
            message_id="msg_live001", correlation_id="corr_live001",
        ),
        peer_priv,
    )
    sent_acks: list = []

    class FakeRelay:
        async def receive_json(self):
            if not hasattr(self, "_sent"):
                self._sent = True
                return {
                    "type": "delivery",
                    "relay_id": "dlv_test001",
                    "envelope": env,
                }
            await asyncio.Event().wait()

        async def send_json(self, frame):
            if frame.get("type") == "delivery_ack":
                sent_acks.append(frame.get("relay_id"))

        async def close(self):
            pass

    async def _fake_connect(*args, **kwargs):
        return FakeRelay()

    monkeypatch.setattr(ask_route.relay_client, "connect", _fake_connect)
    with client.websocket_connect("/ask/live") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        notice = ws.receive_json()
        assert notice["type"] == "delivery"
        assert notice["envelope"]["message_id"] == "msg_live001"
        assert notice["outcome"] == "parked"
    assert sent_acks == ["dlv_test001"]
    pending = client.get("/ask/approvals").json()["approvals"]
    assert [c["correlation_id"] for c in pending] == ["corr_live001"]


def test_live_bridge_auth_failure_closes_4401(tmp_path, monkeypatch):
    import app.api.routes.ask as ask_route
    from app.a2a import relay_client as rc
    from starlette.websockets import WebSocketDisconnect

    client = _live_client(tmp_path, monkeypatch, name="live4401")

    async def _bad_connect(*args, **kwargs):
        raise rc.RelayAuthError("4401", "challenge signature invalid")

    monkeypatch.setattr(ask_route.relay_client, "connect", _bad_connect)
    with client.websocket_connect("/ask/live") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()  # server sends nothing: auth failed, 4401
    assert exc.value.code == 4401


def _free_refused_port():
    """Ephemeral loopback port, closed so connects refuse (never 8000s)."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert port not in (8000, 3000, 8001, 3001)
    return port


def test_ask_returns_fast_when_relay_refused(tmp_path, monkeypatch):
    """RELAY-DOWN HANG: refused relay -> immediate queued (<8s).

    Interactive timeouts stay aggressive (connect <=3s, ack wait <=5s)
    via named constants; the background worker records delivery_failed
    for refresh/retry to observe.
    """
    import time

    from app.a2a import relay_client as rc

    connect_timeout = getattr(rc, "CONNECT_TIMEOUT", getattr(
        rc, "INTERACTIVE_CONNECT_TIMEOUT", None))
    ack_timeout = getattr(rc, "ACK_TIMEOUT", getattr(
        rc, "INTERACTIVE_ACK_TIMEOUT", None))
    assert connect_timeout is not None, "named connect timeout constant"
    assert ack_timeout is not None, "named ack timeout constant"
    assert connect_timeout <= 3.0, connect_timeout
    assert ack_timeout <= 5.0, ack_timeout

    port = _free_refused_port()
    client = _ask_client(
        tmp_path, monkeypatch, name="fastfb",
        relay_url=f"http://127.0.0.1:{port}",
    )
    peer_id = _route_peer(client)
    started = time.monotonic()
    resp = client.post(
        "/ask", json={"peer_agent_id": peer_id, "question": "Summarize?"}
    )
    elapsed = time.monotonic() - started
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery"]["mode"] == "queued"
    assert "background" in body["delivery"].get("reason", "").lower()
    assert elapsed < 8.0, elapsed
    rows = client.get("/ask/messages").json()["messages"]
    assert rows[0]["status"] == "delivery_failed"


def test_live_bridge_tears_down_when_relay_drops(tmp_path, monkeypatch):
    """STALE LIVE BRIDGE: relay drop -> browser closes (or relay_down)."""
    import threading

    import app.api.routes.ask as ask_route
    from app.a2a import envelope as app_envelope
    from app.a2a import relay_client as rc
    from app.identity.service import load_identity
    from app.store import open_db
    from starlette.websockets import WebSocketDisconnect
    from tests.relay_db import new_agent

    client = _live_client(tmp_path, monkeypatch, name="livedrop")
    peer_priv, peer_pub, peer_id = new_agent()
    assert client.post(
        "/pairing/approve",
        json={"card": live_card(peer_priv, peer_id, peer_pub, "Blaise")},
    ).status_code == 200
    conn = open_db(__import__("os").environ["NEXUS_DB_PATH"])
    try:
        local_id = load_identity(
            conn, __import__("os").environ["NEXUS_IDENTITY_KEY"]
        ).agent_id
    finally:
        conn.close()
    env = app_envelope.sign(
        app_envelope.new_envelope(
            sender=peer_id, recipient=local_id, message_type="request",
            payload={"action": "answer", "question": "Relayed hello?",
                     "data_category": "general", "purpose": "answer"},
            timestamp=NOW_ISO, expires_at=LIVE_EXP,
            message_id="msg_drop001", correlation_id="corr_drop001",
        ),
        peer_priv,
    )
    sent_acks: list = []

    class DroppingRelay:
        async def receive_json(self):
            if not hasattr(self, "_sent"):
                self._sent = True
                return {
                    "type": "delivery",
                    "relay_id": "dlv_drop001",
                    "envelope": env,
                }
            raise rc.RelayError("DROPPED", "relay dropped the socket.")

        async def send_json(self, frame):
            if frame.get("type") == "delivery_ack":
                sent_acks.append(frame.get("relay_id"))

        async def close(self):
            pass

    async def _fake_connect(*args, **kwargs):
        return DroppingRelay()

    monkeypatch.setattr(ask_route.relay_client, "connect", _fake_connect)
    with client.websocket_connect("/ask/live") as ws:
        assert ws.receive_json()["type"] == "ready"
        notice = ws.receive_json()
        assert notice["type"] == "delivery"
        assert notice["envelope"]["message_id"] == "msg_drop001"
        # Relay drops right after: the bridge must tear down the
        # browser socket (relay_down event and/or close) promptly.
        box: dict = {}

        def _wait_next():
            try:
                box["frame"] = ws.receive_json()
            except WebSocketDisconnect as exc:
                box["disconnect"] = exc
            except Exception as exc:  # noqa: BLE001 - any close counts
                box["error"] = exc

        waiter = threading.Thread(target=_wait_next, daemon=True)
        waiter.start()
        waiter.join(timeout=8.0)
        assert not waiter.is_alive(), "browser socket never tore down"
        if "frame" in box:
            assert isinstance(box["frame"], dict)
            assert box["frame"].get("type") == "relay_down"
            # After the event the socket must close too.
            box2: dict = {}

            def _wait_close():
                try:
                    box2["frame"] = ws.receive_json()
                except WebSocketDisconnect as exc:
                    box2["disconnect"] = exc
                except Exception as exc:  # noqa: BLE001
                    box2["error"] = exc

            closer = threading.Thread(target=_wait_close, daemon=True)
            closer.start()
            closer.join(timeout=8.0)
            assert not closer.is_alive(), "socket stayed open after relay_down"
            assert "disconnect" in box2 or "error" in box2, box2
        else:
            assert "disconnect" in box or "error" in box, box
    assert sent_acks == ["dlv_drop001"]
