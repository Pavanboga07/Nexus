"""Workflow API: define small step DAGs, run them, watch them converge."""

from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import workflows
from app.auth import OwnerDenied
from app.workflows import WorkflowError

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowIn(BaseModel):
    name: str = ""
    steps: list = []
    requesting_agent: str = "owner"


def _conn_factory():
    from app.store import migrate, open_db

    path = os.environ.get("NEXUS_DB_PATH", "data/nexus.db")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = open_db(path)
    migrate(conn)
    return conn


def get_conn():
    conn = _conn_factory()
    try:
        yield conn
    finally:
        conn.close()


def _error(exc: WorkflowError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status or 400,
        content={"detail": str(exc), "code": exc.code},
    )


def _owned_flow(conn, flow_id: str) -> dict:
    """Workflow the caller owns; otherwise 404 (never confirm others)."""
    from app.auth import OwnerDenied, current_principal

    try:
        flow = workflows.get_workflow(conn, flow_id)
    except WorkflowError:
        raise OwnerDenied()
    if flow["owner_id"] != current_principal().owner_id:
        raise OwnerDenied()
    return flow


def _not_found():
    return JSONResponse(
        status_code=404,
        content={"detail": "Not found.", "code": "NOT_FOUND"},
    )


@router.post("/run")
async def run_workflow_route(
    body: WorkflowIn, conn: sqlite3.Connection = Depends(get_conn)
):
    from app.auth import OwnerDenied, current_principal

    try:
        requesting = (body.requesting_agent or "owner").strip() or "owner"
        if requesting not in ("", "owner"):
            from app import agents

            try:
                agent = agents.resolve_agent(conn, requesting)
            except agents.AgentError:
                return _not_found()
            if agent["owner_id"] != current_principal().owner_id:
                return _not_found()
            requesting = agent["id"]
        flow = workflows.create_workflow(
            conn, name=body.name, steps=body.steps,
            owner_id=current_principal().owner_id)
        return await workflows.run_workflow(
            _conn_factory, flow["id"], requesting_agent=requesting)
    except WorkflowError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.get("")
def list_workflows_route(
    conn: sqlite3.Connection = Depends(get_conn)
):
    from app.auth import current_principal

    mine = current_principal().owner_id
    return {"workflows": [
        w for w in workflows.list_workflows(conn)
        if w["owner_id"] == mine
    ]}


@router.get("/{flow_id}")
def get_workflow_route(
    flow_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        return _owned_flow(conn, flow_id)
    except WorkflowError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.post("/{flow_id}/pause")
def pause_workflow_route(
    flow_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        _owned_flow(conn, flow_id)
        return workflows.pause_workflow(_conn_factory, flow_id)
    except WorkflowError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.post("/{flow_id}/cancel")
def cancel_workflow_route(
    flow_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        _owned_flow(conn, flow_id)
        return workflows.cancel_workflow(_conn_factory, flow_id)
    except WorkflowError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


__all__ = ["router"]
