"""Local agent registry API: many agents, one owner, per-agent keys.

Every route here is bearer-authed by the middleware (operator trust
domain). Responses never carry private key material — only ids,
fingerprints, versions, and statuses.
"""

from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import agents, capabilities
from app.agents import AgentError
from app.capabilities import CapabilityError

router = APIRouter(prefix="/agents", tags=["agents"])


class CreateIn(BaseModel):
    name: str = ""
    display_name: str = ""


class RevokeIn(BaseModel):
    key_id: str | None = None


class ExecuteIn(BaseModel):
    capability_id: str = ""
    args: dict | None = None
    requester_ref: str = "owner"
    purpose: str = ""
    delegation_id: str | None = None
    task_id: str = ""


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


def _secret() -> str:
    from app.machine_config import (
        MachineConfigError,
        get_or_create_identity_secret,
    )

    try:
        return get_or_create_identity_secret()
    except MachineConfigError as exc:
        raise AgentError("NO_IDENTITY", str(exc), status=503) from exc


def _error(exc: AgentError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status or 400,
        content={"detail": str(exc), "code": exc.code},
    )


def _owned(conn, ref: str) -> dict:
    """Resolve an agent the caller owns; unknown-or-foreign → 404."""
    from app.auth import current_principal

    try:
        agent = agents.resolve_agent(conn, ref)
    except AgentError as exc:
        raise exc
    if agent["owner_id"] != current_principal().owner_id:
        raise AgentError("NOT_FOUND", "unknown agent.", status=404)
    return agent


@router.get("")
def list_agents_route(conn: sqlite3.Connection = Depends(get_conn)):
    from app.auth import current_principal

    if current_principal().owner_id == "local":
        agents.ensure_default_agent_quiet(conn, _secret())
    return {"agents": agents.list_agents(
        conn, owner_id=current_principal().owner_id)}


@router.post("")
def create_agent_route(
    body: CreateIn, conn: sqlite3.Connection = Depends(get_conn)
):
    from app.auth import current_principal

    if current_principal().owner_id == "local":
        agents.ensure_default_agent_quiet(conn, _secret())
    try:
        return agents.create_agent(
            conn, _secret(), body.name, body.display_name,
            owner_id=current_principal().owner_id,
        )
    except AgentError as exc:
        return _error(exc)


class CapabilityIn(BaseModel):
    name: str = ""
    version: int = 1
    description: str = ""
    input_schema: dict | None = None
    output_schema: dict | None = None
    tool: str | None = None


def _capability_error(exc: CapabilityError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status or 400,
        content={"detail": str(exc), "code": exc.code},
    )


@router.get("/discover")
def discover_route(q: str = "", fuzzy: bool = False):
    from app import discovery
    from app.auth import current_principal
    from app.store import migrate, open_db

    import os

    path = os.environ.get("NEXUS_DB_PATH", "data/nexus.db")
    conn = open_db(path)
    migrate(conn)
    try:
        out = discovery.resolve(conn, q, fuzzy=fuzzy)
        mine = {a["agent_id"] for a in agents.list_agents(
            conn, owner_id=current_principal().owner_id)}
        out["matches"] = [
            m for m in out["matches"]
            if m.get("kind") != "agent" or m["id"] in mine
        ]
        return out
    finally:
        conn.close()


@router.get("/{ref}/card")
def agent_card_route(
    ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        _owned(conn, ref)
        return capabilities.agent_card(conn, _secret(), ref)
    except AgentError as exc:
        return _error(exc)
    except CapabilityError as exc:
        return _capability_error(exc)


@router.get("/{ref}/capabilities")
def list_capabilities_route(
    ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        _owned(conn, ref)
        return {
            "capabilities": capabilities.list_capabilities(conn, ref)
        }
    except AgentError as exc:
        return _error(exc)
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/{ref}/capabilities")
def register_capability_route(
    body: CapabilityIn,
    ref: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        _owned(conn, ref)
        return capabilities.register_capability(
            conn,
            ref,
            body.name,
            version=body.version,
            description=body.description,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            tool=body.tool,
        )
    except AgentError as exc:
        return _error(exc)
    except CapabilityError as exc:
        return _capability_error(exc)


def _owned_cap(conn, ref: str, cap_id: str) -> dict:
    """Capability must belong to the referenced owned agent."""
    agent = _owned(conn, ref)
    try:
        cap = capabilities.get_capability(conn, cap_id)
    except CapabilityError as exc:
        raise exc
    if cap["agent_id"] != agent["agent_id"]:
        raise AgentError("NOT_FOUND", "unknown capability.", status=404)
    return cap


@router.post("/{ref}/capabilities/{cap_id}/disable")
def disable_capability_route(
    ref: str, cap_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        _owned_cap(conn, ref, cap_id)
        return capabilities.set_capability_status(conn, cap_id, "disabled")
    except AgentError as exc:
        return _error(exc)
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/{ref}/capabilities/{cap_id}/enable")
def enable_capability_route(
    ref: str, cap_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        _owned_cap(conn, ref, cap_id)
        return capabilities.set_capability_status(conn, cap_id, "active")
    except AgentError as exc:
        return _error(exc)
    except CapabilityError as exc:
        return _capability_error(exc)


@router.get("/{ref}")
def get_agent_route(ref: str, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        agent = _owned(conn, ref)
        return agents.get_agent(conn, agent["id"])
    except AgentError as exc:
        return _error(exc)


@router.get("/{ref}/keys")
def agent_keys_route(
    ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        _owned(conn, ref)
        return {"keys": agents.key_history(conn, ref)}
    except AgentError as exc:
        return _error(exc)


@router.post("/{ref}/rotate")
def rotate_agent_route(
    ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        _owned(conn, ref)
        return agents.rotate_agent_key(conn, _secret(), ref)
    except AgentError as exc:
        return _error(exc)


@router.post("/{ref}/revoke")
def revoke_agent_route(
    body: RevokeIn, ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        _owned(conn, ref)
        return agents.revoke_agent_key(conn, ref, body.key_id)
    except AgentError as exc:
        return _error(exc)


@router.post("/{ref}/execute")
async def execute_capability_route(
    body: ExecuteIn, ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    from app.execution import ExecutionError, execute_capability

    try:
        acting = _owned(conn, ref)
        requester = (body.requester_ref or "").strip()
        if requester not in ("", "owner"):
            _owned(conn, requester)
            requester_ref = requester
        else:
            requester_ref = "owner"
        return await execute_capability(
            conn,
            acting_ref=acting["id"],
            capability_id=body.capability_id,
            args=body.args or {},
            requester_ref=requester_ref,
            purpose=body.purpose,
            delegation_id=body.delegation_id,
            task_id=body.task_id,
        )
    except ExecutionError as exc:
        return JSONResponse(
            status_code=exc.status or 400,
            content={"detail": str(exc), "code": exc.code},
        )
    except AgentError as exc:
        return _error(exc)


class AutonomyIn(BaseModel):
    level: str = ""


@router.post("/{ref}/autonomy")
def set_autonomy_route(
    body: AutonomyIn, ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    import datetime

    from app import autonomy as autonomy_mod

    if body.level not in autonomy_mod.AUTONOMY_LEVELS:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "level must be one of"
                          f" {list(autonomy_mod.AUTONOMY_LEVELS)}.",
                "code": "BAD_AUTONOMY",
            },
        )
    try:
        agent = _owned(conn, ref)
        with conn:
            conn.execute(
                "UPDATE agents SET autonomy = ?, updated_at = ?"
                " WHERE agent_id = ?",
                (body.level,
                 datetime.datetime.now(
                     datetime.timezone.utc).isoformat(),
                 agent["agent_id"]),
            )
        view = agents.get_agent(conn, agent["agent_id"])
        view["autonomy"] = body.level
        return view
    except AgentError as exc:
        return _error(exc)


@router.post("/{ref}/keys/{key_id}/revoke")
def revoke_key_route(
    ref: str, key_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        agent = _owned(conn, ref)
        out = agents.revoke_agent_key(conn, agent["id"], key_id)
        return out
    except AgentError as exc:
        return _error(exc)


@router.post("/{ref}/disable")
def disable_agent_route(
    ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    from app import orchestration

    try:
        agent = _owned(conn, ref)
        view = agents.set_agent_status(conn, agent["id"], "disabled")
        reconciled = orchestration.reconcile_agent_tasks(conn, ref)
        view["reconciled_tasks"] = reconciled["failed"]
        return view
    except AgentError as exc:
        return _error(exc)


@router.post("/{ref}/enable")
def enable_agent_route(
    ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        agent = _owned(conn, ref)
        return agents.set_agent_status(conn, agent["id"], "active")
    except AgentError as exc:
        return _error(exc)


__all__ = ["router"]
