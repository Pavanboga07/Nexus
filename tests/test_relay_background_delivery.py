"""Fix #1: non-blocking relay delivery with observable status (TDD red first).

Every ask/approve route used to run the full relay handshake inside the
HTTP request (relay down stalled each click for seconds). The fix queues
a BackgroundTasks worker and returns ``{"mode": "queued"}`` immediately;
the worker flips ``a2a_messages.status`` to ``relayed`` on ack or
``delivery_failed`` on exception (visible via ``GET /ask/messages``),
and ``POST /ask/messages/{id}/retry`` re-queues failed rows.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import struct
import threading
import time

from fastapi.testclient import TestClient
from tests.conftest import auth_headers

LIVE_EXP = "2027-09-22T12:05:00Z"
NOW_ISO = "2026-09-22T12:00:00Z"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FORBIDDEN_PORTS = (8000, 3000, 8001, 3001)


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
    return conn, ident, priv, pub_b64, secret


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


def _ask_client(tmp_path, monkeypatch, name="bg", relay_url=None):
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
    return TestClient(app, headers=auth_headers())


def _route_peer(client, display_name="Blaise"):
    from tests.relay_db import new_agent

    priv, pub_b64, agent_id = new_agent()
    card = live_card(priv, agent_id, pub_b64, display_name)
    resp = client.post("/pairing/approve", json={"card": card})
    assert resp.status_code == 200, resp.text
    return agent_id


def _refused_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert port not in FORBIDDEN_PORTS
    return port


def _wait_message_status(client, message_id, wanted, timeout=15.0):
    """Poll GET /ask/messages until the row reaches a wanted status."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        msgs = client.get("/ask/messages").json()["messages"]
        rows = [m for m in msgs if m["message_id"] == message_id]
        if rows:
            last = rows[0]["status"]
            if last in wanted:
                return last
        time.sleep(0.05)
    raise AssertionError(
        f"message {message_id} never reached {wanted}; last={last!r}"
    )


# --- minimal test relay instance (ephemeral loopback, exact frames) ---------


def _send_frame(conn, payload: bytes):
    head = bytes([0x81])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    conn.sendall(head + payload)


def _recv_frame(conn):
    def _exact(n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    b1, b2 = _exact(2)
    assert b1 & 0x0F == 0x1, f"expected text frame, got {b1:#x}"
    ln = b2 & 0x7F
    if ln == 126:
        (ln,) = struct.unpack("!H", _exact(2))
    elif ln == 127:
        (ln,) = struct.unpack("!Q", _exact(8))
    mask = _exact(4) if b2 & 0x80 else b""
    payload = _exact(ln) if ln else b""
    if mask:
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return json.loads(payload.decode("utf-8"))


class FakeRelay:
    """Ephemeral loopback relay: HTTP upgrade, auth handshake, one
    relay_envelope -> delivery_ack per connection. Never touches the
    dev/prod listener ports."""

    def __init__(self, ack_status="delivered"):
        self.ack_status = ack_status
        self.received: list = []
        self._stop = threading.Event()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        sock.settimeout(0.5)
        assert sock.getsockname()[1] not in FORBIDDEN_PORTS
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self.port

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (OSError, socket.timeout):
                continue
            try:
                self._handle(conn)
            except Exception:  # noqa: BLE001 - test server best-effort
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn):
        conn.settimeout(10)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        head = data.decode("latin1")
        key = re.search(r"Sec-WebSocket-Key:\s*(\S+)", head).group(1)
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        challenge = base64.b64encode(os.urandom(32)).decode()
        _send_frame(
            conn,
            json.dumps(
                {
                    "type": "auth_challenge",
                    "challenge": challenge,
                    "correlation_id": "c1",
                }
            ).encode(),
        )
        req = _recv_frame(conn)
        assert req.get("type") == "auth_response"
        _send_frame(
            conn,
            json.dumps(
                {
                    "type": "auth_result",
                    "success": True,
                    "agent_id": req.get("agent_id"),
                }
            ).encode(),
        )
        env_frame = _recv_frame(conn)
        assert env_frame.get("type") == "relay_envelope"
        self.received.append(env_frame)
        _send_frame(
            conn,
            json.dumps(
                {
                    "type": "delivery_ack",
                    "relay_id": env_frame.get("relay_id"),
                    "status": self.ack_status,
                }
            ).encode(),
        )


# --- (a) ask returns queued without relay contact ----------------------------


def test_ask_returns_queued_when_relay_unreachable(tmp_path, monkeypatch):
    """Unreachable relay -> immediate 200 queued (never a per-click stall,
    never a local-only lie about a queued background delivery)."""
    port = _refused_port()
    client = _ask_client(
        tmp_path, monkeypatch, name="q1",
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
    assert body["envelope"]["message_type"] == "request"
    assert body["delivery"]["mode"] == "queued"
    assert "background" in body["delivery"].get("reason", "").lower()
    assert elapsed < 8.0, elapsed


# --- (b) worker flips status to relayed on ack -------------------------------


def test_background_worker_marks_relayed_on_ack(tmp_path, monkeypatch):
    """Direct worker run against the test relay instance: ack flips the
    stored row from sent to relayed."""
    import app.api.routes.ask as ask_route
    from app import pairing
    from app.store import open_db
    from datetime import datetime, timezone

    conn, ident, priv, pub_b64, secret = make_profile(tmp_path, "w1")
    from tests.relay_db import new_agent

    peer_priv, peer_pub, peer_id = new_agent()
    pairing.approve_peer(
        conn, live_card(peer_priv, peer_id, peer_pub, "Blaise"),
        now=datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc),
    )
    from app.a2a import service

    signed = service.create_request(
        conn, signer_priv=priv, sender_id=ident.agent_id,
        recipient_id=peer_id, question="Summarize?",
        timestamp=NOW_ISO, expires_at=LIVE_EXP,
        message_id="msg_bg001", correlation_id="corr_bg001",
    )
    conn.close()
    fake = FakeRelay()
    port = fake.start()
    try:
        monkeypatch.setenv("NEXUS_RELAY_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("NEXUS_IDENTITY_KEY", secret)
        asyncio.run(
            ask_route._background_deliver(
                signed["message_id"], signed, str(tmp_path / "w1.db")
            )
        )
    finally:
        fake.stop()
    assert [r["envelope"]["message_id"] for r in fake.received] == [
        "msg_bg001"
    ]
    conn = open_db(str(tmp_path / "w1.db"))
    try:
        row = conn.execute(
            "SELECT status FROM a2a_messages WHERE message_id = ?",
            ("msg_bg001",),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "relayed"


def test_ask_end_to_end_relayed_via_background(tmp_path, monkeypatch):
    """POST /ask -> queued; the background worker acks and the row reads
    back relayed via the existing GET /ask/messages."""
    fake = FakeRelay()
    port = fake.start()
    try:
        client = _ask_client(
            tmp_path, monkeypatch, name="q2",
            relay_url=f"http://127.0.0.1:{port}",
        )
        peer_id = _route_peer(client)
        resp = client.post(
            "/ask", json={"peer_agent_id": peer_id, "question": "Summarize?"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["delivery"]["mode"] == "queued"
        message_id = resp.json()["message_id"]
        assert _wait_message_status(
            client, message_id, ("relayed",)
        ) == "relayed"
    finally:
        fake.stop()


# --- (c) failure -> delivery_failed -> retry ---------------------------------


def test_worker_exception_marks_failed_and_retry_succeeds(
    tmp_path, monkeypatch
):
    """Refused relay -> delivery_failed; retry re-queues and, once the
    relay answers, flips to relayed. Unknown/non-failed ids 404."""
    port = _refused_port()
    client = _ask_client(
        tmp_path, monkeypatch, name="q3",
        relay_url=f"http://127.0.0.1:{port}",
    )
    peer_id = _route_peer(client)
    resp = client.post(
        "/ask", json={"peer_agent_id": peer_id, "question": "Summarize?"}
    )
    assert resp.json()["delivery"]["mode"] == "queued"
    message_id = resp.json()["message_id"]
    assert _wait_message_status(
        client, message_id, ("delivery_failed",)
    ) == "delivery_failed"

    # Retry while still down re-queues (stays failed, never 500s).
    retry = client.post(f"/ask/messages/{message_id}/retry")
    assert retry.status_code == 200, retry.text
    assert retry.json()["delivery"]["mode"] == "queued"

    # Relay comes up: retry now succeeds end to end.
    fake = FakeRelay()
    live_port = fake.start()
    try:
        monkeypatch.setenv(
            "NEXUS_RELAY_URL", f"http://127.0.0.1:{live_port}"
        )
        retry2 = client.post(f"/ask/messages/{message_id}/retry")
        assert retry2.status_code == 200, retry2.text
        assert retry2.json()["delivery"]["mode"] == "queued"
        assert _wait_message_status(
            client, message_id, ("relayed",)
        ) == "relayed"
    finally:
        fake.stop()

    # Retry is only for failed rows.
    assert client.post(
        "/ask/messages/msg_nope_missing/retry"
    ).status_code == 404
    assert client.post(
        f"/ask/messages/{message_id}/retry"
    ).status_code == 404
