"""Local ask/approve/answer API (V4): thin glue over ``app.a2a``.

Config from the environment at request time (test-friendly):
``NEXUS_DB_PATH`` (SQLite file), ``NEXUS_IDENTITY_KEY`` (machine-local
secret for signing). No relay calls here: envelopes are stored+signed
locally and carried on the relay wire by the delivery path.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.a2a import relay_client, service
from app.a2a.service import A2AError

router = APIRouter(prefix="/ask", tags=["ask"])


class AskIn(BaseModel):
    peer_agent_id: str
    question: str
    data_category: str = "general"
    purpose: str = "answer"


class IncomingIn(BaseModel):
    envelope: dict[str, Any]


class RuleIn(BaseModel):
    peer: str = "*"
    data_category: str = "*"
    purpose: str = "*"
    action: str = "*"


class RejectIn(BaseModel):
    reason: str = "declined by approver"


class ResponseIn(BaseModel):
    peer_agent_id: str
    correlation_id: str
    answer: str


def _error(exc: A2AError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status or 400,
        content={"detail": str(exc), "code": exc.code},
    )


def get_conn():
    """Per-request SQLite connection (migrated, closed after)."""
    from app.store import migrate, open_db

    path = os.environ.get("NEXUS_DB_PATH", "data/nexus.db")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = open_db(path)
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def _local_key(conn: sqlite3.Connection):
    """Local (private key, agent_id); 503 when identity is missing."""
    import base64

    from app.identity import crypto
    from app.identity.service import IdentityCorruptionError, load_identity

    secret = os.environ.get("NEXUS_IDENTITY_KEY", "")
    if not secret:
        raise A2AError(
            "NO_IDENTITY",
            "identity secret is not configured.",
            status=503,
        )
    try:
        view = load_identity(conn, secret)
    except IdentityCorruptionError as exc:
        raise A2AError("NO_IDENTITY", str(exc), status=503) from exc
    row = conn.execute(
        "SELECT encrypted_private_key FROM identity WHERE id = 1"
    ).fetchone()
    private_raw = crypto.decrypt_private_key(
        row["encrypted_private_key"], secret
    )
    return crypto.load_private_key(private_raw), view.agent_id


def _relay_ws_url(base: str) -> str:
    """HTTP(S) relay base -> ``ws(s)://.../ws`` (ws(s) passthrough)."""
    base = base.strip().rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    if not base.endswith("/ws"):
        base += "/ws"
    return base


def _local_pubkey(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT public_key FROM identity WHERE id = 1"
    ).fetchone()
    return str(row["public_key"]) if row is not None else ""


def _relay_delivery(conn: sqlite3.Connection, envelope: dict) -> dict:
    """Push one signed envelope to the relay; never silent.

    Returns ``{"mode": "relayed", "status": ...}`` on success and
    ``{"mode": "local-only", "reason": ...}`` whenever the relay is
    unconfigured or unreachable (the envelope stays stored locally).
    """
    base = os.environ.get("NEXUS_RELAY_URL", "").strip()
    if not base:
        return {
            "mode": "local-only",
            "reason": "relay is not configured (set NEXUS_RELAY_URL).",
        }
    try:
        from app.identity import crypto

        priv, agent_id = _local_key(conn)
        status = asyncio.run(
            relay_client.deliver_one(
                _relay_ws_url(base),
                envelope,
                recipient=envelope["recipient"],
                agent_id=agent_id,
                public_key_b64=_local_pubkey(conn),
                sign_fn=lambda data: crypto.sign_bytes(priv, data),
            )
        )
        return {"mode": "relayed", "status": status}
    except Exception as exc:  # noqa: BLE001 - fallback must never raise
        return {"mode": "local-only", "reason": f"relay unreachable: {exc}"}


@router.post("")
def ask_route(body: AskIn, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        priv, agent_id = _local_key(conn)
        signed = service.create_request(
            conn,
            signer_priv=priv,
            sender_id=agent_id,
            recipient_id=body.peer_agent_id,
            question=body.question,
            data_category=body.data_category,
            purpose=body.purpose,
        )
        return {
            "message_id": signed["message_id"],
            "correlation_id": signed["correlation_id"],
            "recipient": signed["recipient"],
            "envelope": signed,
            "delivery": _relay_delivery(conn, signed),
        }
    except A2AError as exc:
        return _error(exc)


@router.post("/response")
def response_route(
    body: ResponseIn, conn: sqlite3.Connection = Depends(get_conn)
):
    """Sign an answer (post-approval) and relay it to the requester."""
    try:
        priv, agent_id = _local_key(conn)
        signed = service.send_response(
            conn,
            signer_priv=priv,
            sender_id=agent_id,
            recipient_id=body.peer_agent_id,
            correlation_id=body.correlation_id,
            answer=body.answer,
        )
        return {
            "message_id": signed["message_id"],
            "correlation_id": signed["correlation_id"],
            "recipient": signed["recipient"],
            "envelope": signed,
            "delivery": _relay_delivery(conn, signed),
        }
    except A2AError as exc:
        return _error(exc)


@router.post("/incoming")
def incoming_route(
    body: IncomingIn, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        priv, agent_id = _local_key(conn)
        outcome = service.receive_envelope(
            conn, body.envelope, signer_priv=priv, local_id=agent_id
        )
        return outcome
    except A2AError as exc:
        return _error(exc)


@router.get("/approvals")
def approvals_route(
    status: str = "pending", conn: sqlite3.Connection = Depends(get_conn)
):
    return {"approvals": service.list_approvals(conn, status=status)}


@router.post("/approvals/{approval_id}/approve")
def approve_route(
    approval_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        priv, agent_id = _local_key(conn)
        signed = service.approve_approval(
            conn, approval_id, signer_priv=priv, local_id=agent_id
        )
        return {
            "approval_id": approval_id,
            "status": "approved",
            "envelope": signed,
            "delivery": _relay_delivery(conn, signed),
        }
    except A2AError as exc:
        return _error(exc)


@router.post("/approvals/{approval_id}/reject")
def reject_route(
    approval_id: str,
    body: RejectIn | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        priv, agent_id = _local_key(conn)
        signed = service.reject_approval(
            conn,
            approval_id,
            signer_priv=priv,
            local_id=agent_id,
            reason=(body.reason if body else "declined by approver"),
        )
        return {
            "approval_id": approval_id,
            "status": "rejected",
            "envelope": signed,
            "delivery": _relay_delivery(conn, signed),
        }
    except A2AError as exc:
        return _error(exc)


@router.get("/messages")
def messages_route(
    correlation_id: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"messages": service.list_messages(conn, correlation_id)}


@router.get("/policy")
def policy_list_route(conn: sqlite3.Connection = Depends(get_conn)):
    return {"rules": service.list_rules(conn)}


@router.post("/policy")
def policy_create_route(
    body: RuleIn, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        return service.create_rule(
            conn,
            peer=body.peer,
            data_category=body.data_category,
            purpose=body.purpose,
            action=body.action,
        )
    except A2AError as exc:
        return _error(exc)


@router.delete("/policy/{rule_id}")
def policy_delete_route(
    rule_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    return {"rule_id": rule_id, "removed": service.delete_rule(conn, rule_id)}


async def _pump_relay_deliveries(browser: WebSocket, relay_ws: Any) -> None:
    """Forward relay ``delivery`` frames to the browser.

    Each delivery is ingested via ``receive_envelope`` (creating
    approvals/answers locally), settled with ``delivery_ack`` on the
    relay socket, then forwarded so the chat UI refreshes. Poison
    envelopes are still acked (else the relay redelivers forever) and
    forwarded as errors.
    """
    from app.store import migrate, open_db

    path = os.environ.get("NEXUS_DB_PATH", "data/nexus.db")
    conn = open_db(path)
    migrate(conn)
    try:
        priv, agent_id = _local_key(conn)
        while True:
            frame = await relay_ws.receive_json()
            if not isinstance(frame, dict) or (
                frame.get("type") != "delivery"
            ):
                continue
            envelope = frame.get("envelope", {})
            try:
                outcome = service.receive_envelope(
                    conn, envelope, signer_priv=priv, local_id=agent_id
                )
            except A2AError as exc:
                outcome = {"outcome": "error", "code": exc.code}
            try:
                await relay_client.ack_delivery(
                    relay_ws, frame.get("relay_id", "")
                )
            except Exception:  # noqa: BLE001 - ack is best-effort
                pass
            try:
                await browser.send_json(
                    {
                        "type": "delivery",
                        "relay_id": frame.get("relay_id"),
                        "envelope": envelope,
                        "outcome": outcome.get("outcome"),
                    }
                )
            except Exception:  # noqa: BLE001 - browser went away
                return
    except (asyncio.CancelledError, WebSocketDisconnect):
        raise
    except Exception:  # noqa: BLE001 - relay dropped; handler closes out
        return
    finally:
        conn.close()


@router.websocket("/live")
async def live_bridge(browser: WebSocket):
    """Chat's live socket: holds ONE relay connection for this browser.

    Signed relay handshake with the local key; relay-auth failure closes
    this socket with 4401 too. Relay ``delivery`` frames are ingested,
    acked, and forwarded so approvals/decide refresh from live traffic.
    """
    await browser.accept()
    from app.store import migrate, open_db

    path = os.environ.get("NEXUS_DB_PATH", "data/nexus.db")
    base = os.environ.get("NEXUS_RELAY_URL", "").strip()
    conn = open_db(path)
    migrate(conn)
    try:
        from app.identity import crypto

        priv, agent_id = _local_key(conn)
        pubkey = _local_pubkey(conn)
        sign = lambda data: crypto.sign_bytes(priv, data)  # noqa: E731
    except A2AError:
        conn.close()
        await browser.close(code=relay_client.WS_CLOSE_UNAUTHORIZED)
        return
    conn.close()
    if not base:
        await browser.close(code=relay_client.WS_CLOSE_UNAUTHORIZED)
        return
    try:
        relay_ws = await relay_client.connect(
            _relay_ws_url(base),
            agent_id=agent_id,
            public_key_b64=pubkey,
            sign_fn=sign,
        )
    except relay_client.RelayAuthError:
        await browser.close(code=relay_client.WS_CLOSE_UNAUTHORIZED)
        return
    except Exception:  # noqa: BLE001 - relay down: not an auth failure
        await browser.close(code=1011)
        return
    await browser.send_json({"type": "ready", "agent_id": agent_id})
    pump = asyncio.ensure_future(_pump_relay_deliveries(browser, relay_ws))
    try:
        while True:
            await browser.receive_json()  # pings ignored; close raises
            if pump.done():
                break
    except WebSocketDisconnect:
        pass
    finally:
        if not pump.done():
            pump.cancel()
        try:
            await relay_ws.close()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


__all__ = ["router"]
