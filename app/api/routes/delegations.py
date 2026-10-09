"""Delegation API: issue, inspect, receive, and revoke signed grants.

All routes bearer-authed by the middleware (operator trust domain).
Grants never bypass the recipient's policy engine — see
``app/execution.py`` for the enforcement path.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from app.api.errors import coded_error_response
from pydantic import BaseModel

from app import delegations
from app.delegations import DelegationError

router = APIRouter(prefix="/delegations", tags=["delegations"])


class IssueIn(BaseModel):
    issuer_ref: str = ""
    recipient_agent_id: str = ""
    capability_id: str = ""
    purpose: str = ""
    task_id: str = ""
    constraints: dict | None = None
    ttl_seconds: int = 3600


from app.api.deps import get_conn


def _secret() -> str:
    from app.machine_config import (
        MachineConfigError,
        get_or_create_identity_secret,
    )

    try:
        return get_or_create_identity_secret()
    except MachineConfigError as exc:
        raise DelegationError("NO_IDENTITY", str(exc), status=503) from exc


def _error(exc: DelegationError) -> JSONResponse:
    return coded_error_response(exc)


@router.post("/issue")
def issue_grant_route(
    body: IssueIn, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        return delegations.issue_grant(
            conn,
            _secret(),
            body.issuer_ref,
            body.recipient_agent_id,
            body.capability_id,
            body.purpose,
            task_id=body.task_id,
            constraints=body.constraints,
            ttl_seconds=body.ttl_seconds,
        )
    except DelegationError as exc:
        return _error(exc)


@router.post("/receive")
def receive_grant_route(
    body: dict, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        return delegations.receive_grant(conn, body)
    except DelegationError as exc:
        return _error(exc)


def _owned_grant(conn, grant_id: str) -> dict:
    """Grant touching one of the caller's agents; otherwise 404."""
    from app import agents
    from app.auth import OwnerDenied, current_principal

    try:
        grant = delegations.get_grant(conn, grant_id)
    except DelegationError:
        raise OwnerDenied()
    mine = {a["agent_id"] for a in agents.list_agents(
        conn, owner_id=current_principal().owner_id)}
    if grant["issuer_agent_id"] not in mine \
            and grant["recipient_agent_id"] not in mine:
        raise OwnerDenied()
    return grant


def _not_found():
    return JSONResponse(
        status_code=404,
        content={"detail": "Not found.", "code": "NOT_FOUND"},
    )


@router.get("/{grant_id}")
def get_grant_route(
    grant_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    from app.auth import OwnerDenied

    try:
        return _owned_grant(conn, grant_id)
    except DelegationError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.post("/{grant_id}/revoke")
def revoke_grant_route(
    grant_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    from app.auth import OwnerDenied

    try:
        _owned_grant(conn, grant_id)
        return delegations.revoke_grant(conn, grant_id)
    except DelegationError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


__all__ = ["router"]
