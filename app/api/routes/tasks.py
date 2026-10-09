"""Task API: durable multi-agent task lifecycle over the orchestrator.

Identity comes from the authenticated principal (operator authority);
``requesting_agent_id`` names a local agent the operator acts through
and is validated against the registry — request bodies can never
impersonate anything else. Remote dispatches return DISPATCHED with
the signed envelope; the route schedules background delivery.
"""

from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from app.api.errors import coded_error_response
from pydantic import BaseModel

from app import orchestration, tasks
from app.auth import OwnerDenied, current_principal
from app.dispatch import queue_delivery
from app.tasks import TaskError


def _requesting_or_404(conn, requesting: str) -> str:
    """Requesting agent must be one of the caller's (or owner-self).

    Never trust agent identity from the body alone: unknown or
    foreign agents look like 404.
    """
    from app import agents

    ref = (requesting or "").strip()
    if ref in ("", "owner"):
        return ref
    try:
        agent = agents.resolve_agent(conn, ref)
    except agents.AgentError:
        raise TaskError("NOT_FOUND", "unknown requesting agent.",
                        status=404)
    if agent["owner_id"] != current_principal().owner_id:
        raise TaskError("NOT_FOUND", "unknown requesting agent.",
                        status=404)
    return agent["agent_id"]

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskIn(BaseModel):
    target_agent: str = ""
    capability: str = ""
    purpose: str = "answer"
    input: dict | None = None
    requesting_agent_id: str = "owner"
    parent_task_id: str = ""
    delegation_id: str = ""
    timeout_seconds: int = 300
    idempotency_key: str = ""


from app.api.deps import get_conn


def _db_path() -> str:
    return os.environ.get("NEXUS_DB_PATH", "data/nexus.db")


def _error(exc: TaskError) -> JSONResponse:
    return coded_error_response(exc)


def _detail(task_id: str, conn: sqlite3.Connection) -> dict:
    task = tasks.get_task(conn, task_id)
    if task["owner_id"] != current_principal().owner_id:
        raise OwnerDenied()
    task["children"] = tasks.task_children(conn, task_id)
    task["events"] = tasks.task_events(conn, task_id)
    return task


def _owner_or_404(task_id: str, conn: sqlite3.Connection) -> dict:
    """Fetch a task the caller owns; cross-owner reads look like 404."""
    try:
        return _detail(task_id, conn)
    except OwnerDenied:
        raise TaskError("NOT_FOUND", "unknown task.", status=404)
    except tasks.TaskError:
        raise


@router.post("")
async def create_task_route(
    body: TaskIn, background: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        requesting = _requesting_or_404(conn, body.requesting_agent_id)
        if body.parent_task_id:
            _owner_or_404(body.parent_task_id, conn)
        task = tasks.create_task(
            conn,
            owner_id=current_principal().owner_id,
            requesting_agent_id=requesting,
            target_agent=body.target_agent,
            capability=body.capability,
            purpose=body.purpose,
            task_input=body.input or {},
            parent_task_id=body.parent_task_id,
            delegation_id=body.delegation_id,
            timeout_seconds=body.timeout_seconds,
            idempotency_key=body.idempotency_key,
        )
    except TaskError as exc:
        return _error(exc)
    try:
        final, envelope = await orchestration.run_task(conn, task["task_id"])
    except TaskError as exc:
        return _error(exc)
    if envelope is not None and final["status"] == "DISPATCHED":
        from app import agents

        sender_ref = None
        try:
            sender_ref = agents.resolve_agent(
                conn, final["requesting_agent_id"])["id"]
        except agents.AgentError:
            sender_ref = None
        queue_delivery(background, envelope, _db_path(),
                       sender_ref=sender_ref)
    return _detail(final["task_id"], conn)


@router.get("")
def list_tasks_route(status: str | None = None, limit: int = 50,
                     conn: sqlite3.Connection = Depends(get_conn)):
    tasks.sweep_timeouts(conn)
    return {"tasks": tasks.list_tasks(
        conn, owner_id=current_principal().owner_id,
        status=status, limit=limit)}


@router.get("/{task_id}")
def get_task_route(task_id: str,
                   conn: sqlite3.Connection = Depends(get_conn)):
    tasks.sweep_timeouts(conn)
    try:
        return _owner_or_404(task_id, conn)
    except TaskError as exc:
        return _error(exc)


@router.post("/{task_id}/cancel")
def cancel_task_route(task_id: str,
                      conn: sqlite3.Connection = Depends(get_conn)):
    try:
        _owner_or_404(task_id, conn)
        out = tasks.cancel_task(conn, task_id)
    except TaskError as exc:
        return _error(exc)
    return _detail(out["task_id"], conn)


@router.post("/{task_id}/retry")
async def retry_task_route(task_id: str, background: BackgroundTasks,
                           conn: sqlite3.Connection = Depends(get_conn)):
    try:
        _owner_or_404(task_id, conn)
        fresh = tasks.retry_task(conn, task_id)
        final, envelope = await orchestration.run_task(
            conn, fresh["task_id"])
    except TaskError as exc:
        return _error(exc)
    if envelope is not None and final["status"] == "DISPATCHED":
        queue_delivery(background, envelope, _db_path(),
                       sender_ref=final["requesting_agent_id"] or None)
    return _detail(final["task_id"], conn)


@router.post("/{task_id}/advance")
async def advance_task_route(
        task_id: str, background: BackgroundTasks,
        conn: sqlite3.Connection = Depends(get_conn)):
    try:
        _owner_or_404(task_id, conn)
        final, envelope = await orchestration.advance_task(conn, task_id)
    except TaskError as exc:
        return _error(exc)
    if envelope is not None and final["status"] == "DISPATCHED":
        queue_delivery(background, envelope, _db_path(),
                       sender_ref=final["requesting_agent_id"] or None)
    return _detail(final["task_id"], conn)


__all__ = ["router"]
