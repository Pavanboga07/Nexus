"""Local ask/approve/answer API (V4): thin glue over ``app.a2a``.

Config from the environment at request time (test-friendly):
``NEXUS_DB_PATH`` (SQLite file), ``NEXUS_IDENTITY_KEY`` (machine-local
secret for signing). No relay calls here: envelopes are stored+signed
locally and carried on the relay wire by the delivery path.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.a2a import service
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


__all__ = ["router"]
