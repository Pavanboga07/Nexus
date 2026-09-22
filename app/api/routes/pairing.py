"""Local pairing API (V3): thin glue over ``app.pairing``.

Config from the environment at request time (test-friendly):
``NEXUS_DB_PATH`` (SQLite file), ``NEXUS_RELAY_URL`` (relay base URL),
``NEXUS_IDENTITY_KEY`` (machine-local secret, invite creation only).
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import pairing
from app.pairing import PairingError

router = APIRouter(prefix="/pairing", tags=["pairing"])


class InviteIn(BaseModel):
    card: dict[str, Any]


class ClaimIn(BaseModel):
    code: str


class ApproveIn(BaseModel):
    card: dict[str, Any]


def _error(exc: PairingError) -> JSONResponse:
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


def _relay_base() -> str:
    base = os.environ.get("NEXUS_RELAY_URL", "")
    if not base:
        raise PairingError(
            "NO_RELAY",
            "relay is not configured (set NEXUS_RELAY_URL).",
            status=503,
        )
    return base


def _local_agent_id(conn: sqlite3.Connection) -> str:
    from app.identity.service import IdentityCorruptionError, load_identity

    secret = os.environ.get("NEXUS_IDENTITY_KEY", "")
    if not secret:
        raise PairingError(
            "NO_IDENTITY",
            "identity secret is not configured.",
            status=503,
        )
    try:
        return load_identity(conn, secret).agent_id
    except IdentityCorruptionError as exc:
        raise PairingError("NO_IDENTITY", str(exc), status=503) from exc


@router.post("/invites")
def create_invite_route(
    body: InviteIn, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        card = body.card
        if card.get("agent_id") != _local_agent_id(conn):
            raise PairingError(
                "NOT_LOCAL_CARD",
                "invite cards must belong to the local identity.",
                status=403,
            )
        return pairing.create_invite(
            conn,
            agent_id=card["agent_id"],
            card=card,
            publish_fn=pairing.http_publish_transport(_relay_base()),
        )
    except PairingError as exc:
        return _error(exc)
    except Exception as exc:
        return _error(
            PairingError("RELAY_UNREACHABLE", f"relay is down: {exc}", status=502)
        )


@router.post("/claim")
def claim_route(body: ClaimIn, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        # Fail fast on malformed codes before touching relay config.
        pairing.normalize_code(body.code)
        return pairing.claim_invite(
            conn,
            body.code,
            claim_fn=pairing.http_claim_transport(_relay_base()),
        )
    except PairingError as exc:
        return _error(exc)
    except Exception as exc:
        return _error(
            PairingError("RELAY_UNREACHABLE", f"relay is down: {exc}", status=502)
        )


@router.post("/approve")
def approve_route(
    body: ApproveIn, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        return pairing.approve_peer(conn, body.card)
    except PairingError as exc:
        return _error(exc)


@router.get("/peers")
def peers_route(conn: sqlite3.Connection = Depends(get_conn)):
    return {"peers": pairing.list_peers(conn)}


@router.delete("/peers/{agent_id}")
def unpair_route(agent_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    # Local-only by design: works with the peer unreachable.
    return {"agent_id": agent_id, "removed": pairing.unpair(conn, agent_id)}


__all__ = ["router"]
