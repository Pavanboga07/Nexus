"""Local ask/approve/answer API (V4): thin glue over ``app.a2a``.

Config from the environment at request time (test-friendly):
``NEXUS_DB_PATH`` (SQLite file), ``NEXUS_IDENTITY_KEY`` (machine-local
secret for signing). No relay calls here: envelopes are stored+signed
locally and carried on the relay wire by the delivery path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from app.api.errors import coded_error_response
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
    agent: str | None = None


class RejectIn(BaseModel):
    reason: str = "declined by approver"


class ResponseIn(BaseModel):
    peer_agent_id: str
    correlation_id: str
    answer: str


def _error(exc: A2AError) -> JSONResponse:
    return coded_error_response(exc)


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
    """Local (private key, agent_id); 503 when identity is corrupt.

    Thin route-level wrapper around :func:`app.identity.service.local_key`
    mapping identity errors into the A2A error vocabulary.
    """
    from app.identity.service import IdentityCorruptionError
    from app.identity.service import local_key as _impl

    try:
        return _impl(conn)
    except IdentityCorruptionError as exc:
        raise A2AError("NO_IDENTITY", str(exc), status=503) from exc


# Shared relay delivery lives in app/dispatch.py (orchestrated
# dispatches reuse the exact same path). These aliases keep existing
# imports — including tests — working unchanged.
from app.dispatch import (
    QUEUED_DELIVERY,
    deliver_envelope as _background_deliver,
)
from app.dispatch import (
    queue_delivery as _queue_delivery,
)
from app.dispatch import (
    relay_ws_url as _relay_ws_url,
)


def _local_pubkey(conn: sqlite3.Connection) -> str:
    """Local public key (base64); thin wrapper around the identity service."""
    from app.identity.service import local_pubkey as _impl

    return _impl(conn)


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
    body: IncomingIn,
    background: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        priv, agent_id = _local_key(conn)
        outcome = service.receive_envelope(
            conn, body.envelope, signer_priv=priv, local_id=agent_id
        )
        # Remote-executed task delegations come back with a signed reply
        # envelope in the outcome — deliver it to the originator in the
        # background (same path as response_route).
        reply = outcome.get("reply") if isinstance(outcome, dict) else None
        if isinstance(reply, dict) and reply.get("message_id"):
            _queue_delivery(
                background,
                reply,
                os.environ.get("NEXUS_DB_PATH", "data/nexus.db"),
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
        from app.events import emit_best_effort

        row = conn.execute(
            "SELECT correlation_id FROM a2a_approvals"
            " WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        emit_best_effort(
            conn, "approval.approved",
            {"approval_id": approval_id,
             "correlation_id": row["correlation_id"]
             if row is not None else ""},
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
        from app.events import emit_best_effort

        emit_best_effort(
            conn, "approval.rejected",
            {"approval_id": approval_id},
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
    # An all-wildcard ALLOW rule matches everything: refuse the
    # empty-body footgun instead of minting it.
    fields = (
        (body.peer or "").strip(),
        (body.data_category or "").strip(),
        (body.purpose or "").strip(),
        (body.action or "").strip(),
        (body.agent or "").strip() if body.agent else "",
    )
    if all(value in ("", "*") for value in fields):
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Refusing wildcard rule: set at least one of "
                "peer, data_category, purpose, action, or agent to a "
                "non-wildcard value.",
                "code": "WILDCARD_RULE",
            },
        )
    try:
        return service.create_rule(
            conn,
            peer=body.peer,
            data_category=body.data_category,
            purpose=body.purpose,
            action=body.action,
            agent=body.agent,
        )
    except A2AError as exc:
        return _error(exc)


@router.delete("/policy/{rule_id}")
def policy_delete_route(
    rule_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    return {"rule_id": rule_id, "removed": service.delete_rule(conn, rule_id)}


async def ingest_relay_delivery(
    conn: sqlite3.Connection,
    relay_ws: Any,
    frame: dict[str, Any],
    priv: Any,
    agent_id: str,
) -> dict[str, Any]:
    """Ingest ONE relay ``delivery`` frame: ``receive_envelope`` → ack.

    Shared verbatim by the browser live bridge and the background
    relay listener: each delivery is ingested (creating
    approvals/answers locally) and settled with ``delivery_ack`` on the
    relay socket. Poison envelopes are still acked (else the relay
    redelivers forever). Returns the outcome for the caller to fan out.
    """
    envelope = frame.get("envelope", {})
    try:
        outcome = service.receive_envelope(
            conn, envelope, signer_priv=priv, local_id=agent_id
        )
    except A2AError as exc:
        outcome = {"outcome": "error", "code": exc.code}
        logging.getLogger("nexus.ingest").warning(
            "delivery ingest rejected: %s (relay_id=%s)",
            exc.code,
            frame.get("relay_id", ""),
        )
    try:
        await relay_client.ack_delivery(relay_ws, frame.get("relay_id", ""))
    except Exception:  # noqa: BLE001 - ack is best-effort
        pass
    # A remotely-executed task delegation carries its signed reply in the
    # outcome — deliver it back to the originator without blocking the pump.
    reply = outcome.get("reply") if isinstance(outcome, dict) else None
    if isinstance(reply, dict) and reply.get("message_id"):
        from app.dispatch import deliver_envelope as _deliver_reply

        asyncio.create_task(
            _deliver_reply(
                reply["message_id"],
                reply,
                os.environ.get("NEXUS_DB_PATH", "data/nexus.db"),
            )
        )
    return {
        "relay_id": frame.get("relay_id"),
        "envelope": envelope,
        "outcome": outcome.get("outcome"),
    }


async def _pump_relay_deliveries(browser: WebSocket, relay_ws: Any) -> None:
    """Forward relay ``delivery`` frames to the browser.

    Each delivery is ingested via :func:`ingest_relay_delivery`, then
    the outcome is forwarded so approvals/decide refresh from live
    traffic.
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
            outcome = await ingest_relay_delivery(
                conn, relay_ws, frame, priv, agent_id
            )
            try:
                await browser.send_json({"type": "delivery", **outcome})
            except Exception:  # noqa: BLE001 - browser went away
                return
    except (asyncio.CancelledError, WebSocketDisconnect):
        raise
    except Exception:  # noqa: BLE001 - relay dropped; handler closes out
        return
    finally:
        conn.close()


@router.post("/live-ticket")
def live_ticket_route(request: Request):
    """Mint a single-use ticket for the browser live bridge (this route
    itself is bearer-authed by the middleware).

    MVP rate limit: 30 tickets per minute per client.
    """
    from app.auth import client_ip, issue_live_ticket
    from app.ratelimit import check_rate_limit

    client = client_ip(request)
    allowed, retry = check_rate_limit(f"live-ticket:{client}", limit=30, window_seconds=60)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many ticket requests. Try again later.", "code": "RATE_LIMITED"},
            headers={"Retry-After": str(retry)},
        )
    return {"ticket": issue_live_ticket()}


def _live_ticket_from(browser: WebSocket) -> str:
    """Read the live-bridge ticket off the handshake.

    Prefer the ``Sec-WebSocket-Protocol`` header (browsers can set the
    subprotocol on ``new WebSocket(url, [ticket])``); fall back to the
    legacy ``?ticket=`` query param, which still lands in access logs.
    """
    offered = browser.headers.get("sec-websocket-protocol", "")
    first = offered.split(",")[0].strip()
    if first:
        return first
    return browser.query_params.get("ticket", "")


@router.websocket("/live")
async def live_bridge(browser: WebSocket):
    """Chat's live socket: holds ONE relay connection for this browser.

    Browsers cannot send Authorization on the handshake, so auth is a
    single-use ticket minted at ``POST /ask/live-ticket`` (the auth
    middleware never sees websocket scopes — enforced here with 4401).
    The ticket travels in the ``Sec-WebSocket-Protocol`` header when
    the client offers it (keeps the secret out of the URL and the
    access logs); the legacy ``?ticket=`` query param still works.
    Signed relay handshake with the local key; relay-auth failure
    closes this socket with 4401 too. Relay ``delivery`` frames are
    ingested, acked, and forwarded so approvals/decide refresh from
    live traffic.
    """
    await browser.accept()
    from app.auth import redeem_live_ticket

    if not redeem_live_ticket(_live_ticket_from(browser)):
        await browser.close(code=relay_client.WS_CLOSE_UNAUTHORIZED)
        return
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
        # No relay configured: a relay-side problem (1011), not an
        # authentication failure — 4401 would blame the credentials.
        await browser.close(code=1011)
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


__all__ = ["ingest_relay_delivery", "router"]
