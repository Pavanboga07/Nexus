"""Relay WebSocket client (V4 delivery path).

Speaks the exact frame protocol in ``relay/main.py`` — read that file
first, do not guess field names. Summary (mirrored here):

- server sends ``auth_challenge`` {challenge, correlation_id}; the
  client signs the RAW challenge bytes locally and replies
  ``auth_response`` {agent_id, public_key, signature, challenge};
- server replies ``auth_result`` {success, ...} (``success: False``
  followed by close 4401 on any failure);
- ``relay_envelope`` {relay_id, recipient, envelope} -> server answers
  ``delivery_ack`` {relay_id, status: queued|delivered};
- incoming ``delivery`` {relay_id, envelope} is settled by sending
  ``delivery_ack`` {relay_id} back on the same socket.

Flush stays server-side: the relay redelivers unacked rows on
(re)connect, so this client never replays history itself.

Transport-agnostic core (``authenticate`` / ``send_envelope`` /
``ack_delivery`` take any object with async ``send_json`` /
``receive_json``, e.g. a Starlette test socket) plus a stdlib-only
connector (``asyncio`` streams; no new dependencies) for production.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ssl
import struct
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_CLOSE_UNAUTHORIZED = 4401

SignFn = Callable[[bytes], bytes | Awaitable[bytes]]


class RelayError(Exception):
    """Relay transport/protocol failure with machine ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = str(code)


class RelayAuthError(RelayError):
    """Handshake rejected (bad signature -> the server closes 4401)."""


class ConnectionClosed(RelayError):
    """The socket closed (carries the numeric close ``code``)."""


def _new_relay_id() -> str:
    return f"rl_{uuid.uuid4().hex}"


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def _close_code(exc: BaseException) -> str | None:
    code = getattr(exc, "code", None)
    return str(code) if code is not None else None


async def _expect_close_code(ws: Any) -> str | None:
    """Wait for the socket teardown; return its close code, if visible."""
    try:
        await ws.receive_json()
    except BaseException as exc:  # noqa: BLE001 - any close shape counts
        return _close_code(exc)
    return None


async def authenticate(
    ws: Any,
    *,
    agent_id: str,
    public_key_b64: str,
    sign_fn: SignFn,
) -> str:
    """Run the signed handshake; return the authed agent_id.

    Raises :class:`RelayAuthError` (bad signature surfaces the server's
    4401 close) on any failure.
    """
    try:
        frame = await ws.receive_json()
    except BaseException as exc:  # noqa: BLE001 - teardown during hello
        raise RelayAuthError(
            _close_code(exc) or str(WS_CLOSE_UNAUTHORIZED),
            f"relay closed before the handshake: {exc}",
        ) from exc
    if (
        not isinstance(frame, dict)
        or frame.get("type") != "auth_challenge"
        or not isinstance(frame.get("challenge"), str)
    ):
        raise RelayAuthError(
            str(WS_CLOSE_UNAUTHORIZED),
            "expected auth_challenge as the first relay frame.",
        )
    challenge_b64 = frame["challenge"]
    try:
        raw = base64.b64decode(challenge_b64.encode("ascii"), validate=True)
    except Exception as exc:
        raise RelayAuthError(
            str(WS_CLOSE_UNAUTHORIZED), "relay challenge is not base64."
        ) from exc
    signature = await _maybe_await(sign_fn(raw))
    await ws.send_json(
        {
            "type": "auth_response",
            "agent_id": agent_id,
            "public_key": public_key_b64,
            "signature": base64.b64encode(bytes(signature)).decode("ascii"),
            "challenge": challenge_b64,
            "correlation_id": frame.get("correlation_id"),
        }
    )
    try:
        result = await ws.receive_json()
    except BaseException as exc:  # noqa: BLE001 - closed instead of result
        raise RelayAuthError(
            _close_code(exc) or str(WS_CLOSE_UNAUTHORIZED),
            f"relay closed during auth: {exc}",
        ) from exc
    if (
        not isinstance(result, dict)
        or result.get("type") != "auth_result"
        or result.get("success") is not True
    ):
        detail = result.get("code", "UNAUTHENTICATED") if isinstance(
            result, dict
        ) else "UNAUTHENTICATED"
        close_code = await _expect_close_code(ws)
        raise RelayAuthError(
            close_code or str(WS_CLOSE_UNAUTHORIZED),
            f"relay rejected auth ({detail}); socket closed "
            f"({close_code or WS_CLOSE_UNAUTHORIZED}).",
        )
    return result.get("agent_id", agent_id)


async def send_envelope(
    ws: Any,
    *,
    envelope: dict[str, Any],
    recipient: str,
    relay_id: str | None = None,
    timeout: float = 20.0,
) -> str:
    """Send one envelope; return the ``delivery_ack`` status.

    Non-matching frames (e.g. flushed deliveries racing our send) are
    skipped, never acked here: the server keeps them unacked and
    redelivers on the next connect, so nothing is lost.
    """
    rid = relay_id or _new_relay_id()
    await ws.send_json(
        {
            "type": "relay_envelope",
            "relay_id": rid,
            "recipient": recipient,
            "envelope": envelope,
        }
    )

    async def _wait() -> str:
        while True:
            frame = await ws.receive_json()
            if (
                isinstance(frame, dict)
                and frame.get("type") == "delivery_ack"
                and frame.get("relay_id") == rid
            ):
                return str(frame.get("status", "delivered"))

    try:
        return await asyncio.wait_for(_wait(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise RelayError(
            "ACK_TIMEOUT", f"no delivery_ack for {rid} within {timeout}s."
        ) from exc


async def ack_delivery(ws: Any, relay_id: str) -> None:
    """Settle one incoming ``delivery`` on the same socket."""
    await ws.send_json({"type": "delivery_ack", "relay_id": relay_id})


# --- stdlib connector (production; no new dependencies) -----------------------


class StdWs:
    """Minimal RFC6455 client over asyncio streams (JSON text frames)."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._buf = b""
        self._closed = False

    async def send_json(self, frame: dict[str, Any]) -> None:
        await self._send_text(json.dumps(frame).encode("utf-8"))

    async def receive_json(self) -> Any:
        while True:
            opcode, payload = await self._recv_frame()
            if opcode == 0x1:  # text
                return json.loads(payload.decode("utf-8"))
            if opcode == 0x8:  # close
                code = (
                    struct.unpack("!H", payload[:2])[0]
                    if len(payload) >= 2
                    else 1005
                )
                self._closed = True
                raise ConnectionClosed(str(code), "relay closed the socket.")
            if opcode == 0x9:  # ping -> pong
                await self._send_frame(0xA, payload)
            elif opcode == 0xA:  # pong
                continue
            elif opcode == 0x2:
                raise RelayError("BINARY_FRAME", "relay sent a binary frame.")
            # Continuation (0x0) is consumed inside _recv_frame.

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._send_frame(0x8, b"")
        except Exception:  # noqa: BLE001 - close is best-effort
            pass
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001 - close is best-effort
            pass

    async def _send_text(self, raw: bytes) -> None:
        await self._send_frame(0x1, raw, mask=True)

    async def _send_frame(
        self, opcode: int, payload: bytes, *, mask: bool = True
    ) -> None:
        head = bytes([0x80 | opcode])
        length = len(payload)
        mask_bit = 0x80 if mask else 0
        if length < 126:
            head += bytes([mask_bit | length])
        elif length < 65536:
            head += bytes([mask_bit | 126]) + struct.pack("!H", length)
        else:
            head += bytes([mask_bit | 127]) + struct.pack("!Q", length)
        if mask:
            mask_key = os.urandom(4)
            head += mask_key
            payload = bytes(
                b ^ mask_key[i % 4] for i, b in enumerate(payload)
            )
        self._writer.write(head + payload)
        await self._writer.drain()

    async def _recv_exactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await self._reader.read(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionClosed("1006", "relay dropped the socket.")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    async def _recv_frame(self) -> tuple[int, bytes]:
        first, second = await self._recv_exactly(2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", await self._recv_exactly(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", await self._recv_exactly(8))
        mask_key = await self._recv_exactly(4) if masked else b""
        payload = await self._recv_exactly(length) if length else b""
        if masked:
            payload = bytes(
                b ^ mask_key[i % 4] for i, b in enumerate(payload)
            )
        if opcode == 0x0:  # pragma: no cover - servers rarely fragment
            raise RelayError("FRAGMENTED", "fragmented frames unsupported.")
        return opcode, payload


async def open_connection(url: str, *, timeout: float = 10.0) -> StdWs:
    """Open a ``ws(s)://`` URL and complete the HTTP upgrade (stdlib)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise RelayError(
            "BAD_URL", f"relay URL must be ws(s)://, got {url!r}."
        )
    host = parts.hostname or "localhost"
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    path = parts.path or "/ws"
    if parts.query:
        path += f"?{parts.query}"
    ssl_ctx = ssl.create_default_context() if parts.scheme == "wss" else None

    async def _open() -> StdWs:
        reader, writer = await asyncio.open_connection(
            host, port, ssl=ssl_ctx
        )
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_hdr = host if (
            (parts.scheme == "ws" and port == 80)
            or (parts.scheme == "wss" and port == 443)
        ) else f"{host}:{port}"
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_hdr}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(4096)
            if not chunk:
                raise RelayError(
                    "HANDSHAKE_FAILED", "relay closed during upgrade."
                )
            head += chunk
            if len(head) > 16384:
                raise RelayError(
                    "HANDSHAKE_FAILED", "relay upgrade headers too large."
                )
        status, _, headers = head.partition(b"\r\n\r\n")[0].partition(b"\r\n")
        if b"101" not in status:
            raise RelayError(
                "HANDSHAKE_FAILED",
                f"relay upgrade rejected: {status.decode('latin1')}.",
            )
        accept = None
        for line in headers.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-accept":
                accept = value.strip().decode("ascii")
        expected = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected:
            raise RelayError(
                "HANDSHAKE_FAILED", "relay upgrade accept key mismatch."
            )
        ws = StdWs(reader, writer)
        ws._buf = bytes(head.partition(b"\r\n\r\n")[2])
        return ws

    try:
        return await asyncio.wait_for(_open(), timeout=timeout)
    except RelayError:
        raise
    except Exception as exc:
        raise RelayError(
            "UNREACHABLE", f"cannot reach the relay at {url}: {exc}"
        ) from exc


async def connect(
    url: str,
    *,
    agent_id: str,
    public_key_b64: str,
    sign_fn: SignFn,
    timeout: float = 10.0,
) -> StdWs:
    """Open + authenticate one relay socket (closes it on auth failure)."""
    ws = await open_connection(url, timeout=timeout)
    try:
        await authenticate(
            ws, agent_id=agent_id, public_key_b64=public_key_b64,
            sign_fn=sign_fn,
        )
    except Exception:
        await ws.close()
        raise
    return ws


async def deliver_one(
    url: str,
    envelope: dict[str, Any],
    *,
    recipient: str,
    agent_id: str,
    public_key_b64: str,
    sign_fn: SignFn,
    relay_id: str | None = None,
    timeout: float = 10.0,
) -> str:
    """Connect, send one envelope, return the ``delivery_ack`` status."""
    ws = await connect(
        url, agent_id=agent_id, public_key_b64=public_key_b64,
        sign_fn=sign_fn, timeout=timeout,
    )
    try:
        return await send_envelope(
            ws, envelope=envelope, recipient=recipient,
            relay_id=relay_id, timeout=timeout + 10.0,
        )
    finally:
        await ws.close()


__all__ = [
    "WS_CLOSE_UNAUTHORIZED",
    "ConnectionClosed",
    "RelayAuthError",
    "RelayError",
    "StdWs",
    "ack_delivery",
    "authenticate",
    "connect",
    "deliver_one",
    "open_connection",
    "send_envelope",
]
