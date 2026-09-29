"""Local ask/approve/answer API (V4): thin glue over ``app.a2a``.

Config from the environment at request time (test-friendly):
``NEXUS_DB_PATH`` (SQLite file), ``NEXUS_IDENTITY_KEY`` (machine-local
secret for signing). No relay calls here: envelopes are stored+signed
locally and carried on the relay wire by the delivery path.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.a2a import relay_client, service
from app.a2a.service import A2AError

router = APIRouter(prefix="/ask", tags=["ask"])

# Poll interval for the live-bridge pump watch (relay drop -> browser
# teardown). Small so the UI flips to disconnected promptly.
LIVE_PUMP_POLL_INTERVAL = 0.5


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
    conn = _request_conn()
    try:
        yield conn
    finally:
        conn.close()


def _request_conn() -> sqlite3.Connection:
    """Open a migrated SQLite connection on the CALLING thread.

    Async routes use this directly: a ``Depends(get_conn)`` connection
    is created in a worker thread, but ``async def`` bodies run in the
    event-loop thread, and SQLite refuses cross-thread use.
    """
    from app.store import migrate, open_db

    path = os.environ.get("NEXUS_DB_PATH", "data/nexus.db")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = open_db(path)
    migrate(conn)
    return conn


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


QUEUED_DELIVERY = {
    "mode": "queued",
    "reason": "delivering in background; refresh to confirm",
}


async def _background_deliver(
    message_id: str, envelope: dict, db_path: str
) -> None:
    """Deliver one stored envelope; flip its row to relayed/failed.

    Opens a FRESH connection (never the request ``conn``) so the HTTP
    request never waits on the relay handshake. Ack -> ``relayed``;
    any exception -> ``delivery_failed`` (both readable via
    ``GET /ask/messages``).
    """
    from app.store import migrate, open_db

    conn = open_db(db_path)
    migrate(conn)
    try:
        try:
            from app.identity import crypto

            base = os.environ.get("NEXUS_RELAY_URL", "").strip()
            priv, agent_id = _local_key(conn)
            await relay_client.deliver_one(
                _relay_ws_url(base),
                envelope,
                recipient=envelope["recipient"],
                agent_id=agent_id,
                public_key_b64=_local_pubkey(conn),
                sign_fn=lambda data: crypto.sign_bytes(priv, data),
                timeout=relay_client.CONNECT_TIMEOUT,
                ack_timeout=relay_client.ACK_TIMEOUT,
            )
            status = "relayed"
        except Exception:  # noqa: BLE001 - failure is a row state
            status = "delivery_failed"
        with conn:
            conn.execute(
                "UPDATE a2a_messages SET status = ? WHERE message_id = ?",
                (status, message_id),
            )
    finally:
        conn.close()


def _queue_delivery(
    background: BackgroundTasks, signed: dict, db_path: str
) -> dict:
    """Return immediately; deliver in the background when configured.

    No relay URL -> ``local-only`` (nothing to send to). Otherwise the
    response is ``queued`` and ``_background_deliver`` flips the stored
    row to ``relayed`` / ``delivery_failed`` for polling.
    """
    base = os.environ.get("NEXUS_RELAY_URL", "").strip()
    if not base:
        return {
            "mode": "local-only",
            "reason": "relay is not configured (set NEXUS_RELAY_URL).",
        }
    background.add_task(
        _background_deliver, signed["message_id"], signed, db_path
    )
    return dict(QUEUED_DELIVERY)


@router.post("")
async def ask_route(body: AskIn, background: BackgroundTasks):
    conn = _request_conn()
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
            "delivery": _queue_delivery(
                background,
                signed,
                os.environ.get("NEXUS_DB_PATH", "data/nexus.db"),
            ),
        }
    except A2AError as exc:
        return _error(exc)
    finally:
        conn.close()


@router.post("/response")
async def response_route(body: ResponseIn, background: BackgroundTasks):
    """Sign an answer (post-approval); delivery happens in background."""
    conn = _request_conn()
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
            "delivery": _queue_delivery(
                background,
                signed,
                os.environ.get("NEXUS_DB_PATH", "data/nexus.db"),
            ),
        }
    except A2AError as exc:
        return _error(exc)
    finally:
        conn.close()


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
async def approve_route(approval_id: str, background: BackgroundTasks):
    conn = _request_conn()
    try:
        priv, agent_id = _local_key(conn)
        signed = service.approve_approval(
            conn, approval_id, signer_priv=priv, local_id=agent_id
        )
        return {
            "approval_id": approval_id,
            "status": "approved",
            "envelope": signed,
            "delivery": _queue_delivery(
                background,
                signed,
                os.environ.get("NEXUS_DB_PATH", "data/nexus.db"),
            ),
        }
    except A2AError as exc:
        return _error(exc)
    finally:
        conn.close()


@router.post("/approvals/{approval_id}/reject")
async def reject_route(
    approval_id: str,
    background: BackgroundTasks,
    body: RejectIn | None = None,
):
    conn = _request_conn()
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
            "delivery": _queue_delivery(
                background,
                signed,
                os.environ.get("NEXUS_DB_PATH", "data/nexus.db"),
            ),
        }
    except A2AError as exc:
        return _error(exc)
    finally:
        conn.close()


@router.get("/messages")
def messages_route(
    correlation_id: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"messages": service.list_messages(conn, correlation_id)}


@router.post("/messages/{message_id}/retry")
async def retry_route(message_id: str, background: BackgroundTasks):
    """Re-queue background delivery for a ``delivery_failed`` row."""
    conn = _request_conn()
    try:
        row = conn.execute(
            "SELECT message_id, envelope_json, status FROM a2a_messages "
            "WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None or row["status"] != "delivery_failed":
            return JSONResponse(
                status_code=404,
                content={"detail": "message not found or not failed."},
            )
        background.add_task(
            _background_deliver,
            row["message_id"],
            json.loads(row["envelope_json"]),
            os.environ.get("NEXUS_DB_PATH", "data/nexus.db"),
        )
        return {
            "message_id": row["message_id"],
            "delivery": dict(QUEUED_DELIVERY),
        }
    finally:
        conn.close()


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
            if pump.done():
                # Relay dropped: the pump already returned, so tell the
                # UI and close — else the bridge sits in receive_json
                # forever showing "on" with no reconnect.
                try:
                    await browser.send_json(
                        {
                            "type": "relay_down",
                            "reason": (
                                "relay unreachable: connection lost."
                            ),
                        }
                    )
                except Exception:  # noqa: BLE001 - browser went away
                    pass
                try:
                    await browser.close(code=1011)
                except Exception:  # noqa: BLE001 - close best-effort
                    pass
                break
            try:
                await asyncio.wait_for(
                    browser.receive_json(),
                    timeout=LIVE_PUMP_POLL_INTERVAL,
                )  # pings ignored; close raises
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        pass
    finally:
        if not pump.done():
            pump.cancel()
        else:
            try:
                pump.exception()
            except Exception:  # noqa: BLE001 - pump outcome already handled
                pass
        try:
            await relay_ws.close()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


__all__ = ["router"]
