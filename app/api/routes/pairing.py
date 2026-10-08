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
    # Primary path sends NO card: the server builds it from the local
    # key. A supplied card is only a legacy/defense path and must still
    # belong to the local identity (else NOT_LOCAL_CARD).
    card: dict[str, Any] | None = None
    display_name: str | None = None


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
    raw = os.environ.get("NEXUS_RELAY_URL", "").strip()
    if not raw:
        raise PairingError(
            "NO_RELAY",
            "relay is not configured (set NEXUS_RELAY_URL).",
            status=503,
        )
    # NEXUS_RELAY_URL is the WS(S) URL (the delivery path needs it), but
    # pairing talks plain HTTPS: normalize the scheme here instead of
    # POSTing to a wss:// URL (the relay 500s those).
    if raw.startswith("wss://"):
        return "https://" + raw[len("wss://"):]
    if raw.startswith("ws://"):
        return "http://" + raw[len("ws://"):]
    return raw


def _local_agent_id(conn: sqlite3.Connection) -> str:
    from app.identity.service import IdentityCorruptionError, ensure_identity
    from app.machine_config import MachineConfigError, get_or_create_identity_secret

    try:
        secret = get_or_create_identity_secret()
    except MachineConfigError as exc:
        raise PairingError("NO_IDENTITY", str(exc), status=503) from exc
    try:
        # ensure: first pairing call initializes the identity; NO_IDENTITY
        # survives only for genuinely corrupt stores.
        return ensure_identity(conn, secret).agent_id
    except IdentityCorruptionError as exc:
        raise PairingError("NO_IDENTITY", str(exc), status=503) from exc


@router.post("/invites")
def create_invite_route(
    body: InviteIn, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        if body.card is not None:
            card = body.card
            if card.get("agent_id") != _local_agent_id(conn):
                raise PairingError(
                    "NOT_LOCAL_CARD",
                    "invite cards must belong to the local identity.",
                    status=403,
                )
            agent_id = card["agent_id"]
        else:
            from app.machine_config import (
                MachineConfigError,
                get_or_create_identity_secret,
            )

            try:
                secret = get_or_create_identity_secret()
            except MachineConfigError as exc:
                raise PairingError("NO_IDENTITY", str(exc), status=503) from exc
            card = pairing.build_local_card(
                conn,
                secret,
                display_name=body.display_name,
                endpoint=os.environ.get("NEXUS_AGENT_ENDPOINT")
                or pairing.DEFAULT_AGENT_ENDPOINT,
            )
            agent_id = card["agent_id"]
        return pairing.create_invite(
            conn,
            agent_id=agent_id,
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


@router.get("/me")
def my_identity_route(conn: sqlite3.Connection = Depends(get_conn)):
    """Local identity card: agent_id, public key, fingerprint.

    503 ``NO_IDENTITY`` when no machine secret is configured or the
    stored key is corrupt — same shape as the other identity faults.
    """
    from app.identity.service import IdentityCorruptionError, ensure_identity
    from app.machine_config import MachineConfigError, get_or_create_identity_secret

    try:
        try:
            secret = get_or_create_identity_secret()
        except MachineConfigError as exc:
            raise PairingError("NO_IDENTITY", str(exc), status=503) from exc
        try:
            view = ensure_identity(conn, secret)
        except IdentityCorruptionError as exc:
            raise PairingError("NO_IDENTITY", str(exc), status=503) from exc
        return {
            "agent_id": view.agent_id,
            "public_key": view.public_key,
            "fingerprint": view.fingerprint,
        }
    except PairingError as exc:
        return _error(exc)


@router.get("/peers")
def peers_route(conn: sqlite3.Connection = Depends(get_conn)):
    return {"peers": pairing.list_peers(conn)}


@router.delete("/peers/{agent_id}")
def unpair_route(agent_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    # Local-only by design: works with the peer unreachable.
    return {"agent_id": agent_id, "removed": pairing.unpair(conn, agent_id)}


class TrustIn(BaseModel):
    state: str = ""


@router.get("/directory/lookup")
def directory_lookup_route(agent_id: str):
    """Fetch + verify one gateway directory card (discovery metadata).

    Verified cards only — anything failing verification is rejected,
    never trusted. Pairing still requires the invite ceremony.
    """
    from app import remote_directory

    try:
        return {"card": remote_directory.fetch_card(agent_id)}
    except remote_directory.DirectoryError as exc:
        return JSONResponse(
            status_code=exc.status or 502,
            content={"detail": str(exc), "code": exc.code},
        )


@router.post("/peers/{agent_id}/trust")
def set_trust_route(agent_id: str, body: TrustIn,
                    conn: sqlite3.Connection = Depends(get_conn)):
    """Move a peer through TRUSTED → SUSPENDED → REVOKED.

    Suspended peers exchange nothing new (history kept); revoked peers
    additionally fail delegation verification. Unpair deletes the row.
    """
    try:
        return pairing.set_peer_trust(conn, agent_id, body.state)
    except pairing.PairingError as exc:
        return _error(exc)


__all__ = ["router"]
