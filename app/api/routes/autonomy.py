"""Autonomy API: schedules, triggers, events. All bearer-authed."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from app.api.errors import coded_error_response
from pydantic import BaseModel

from app import autonomy
from app.auth import OwnerDenied
from app.autonomy import AutonomyError

router = APIRouter(prefix="/autonomy", tags=["autonomy"])


class ScheduleIn(BaseModel):
    agent_id: str = ""
    name: str = ""
    kind: str = "cron"
    trigger: str = ""
    timezone: str = "UTC"
    target_agent: str = ""
    capability: str = ""
    task_input: dict | None = None
    purpose: str = "answer"
    delegation_id: str = ""
    timeout_seconds: int = 300
    enabled: bool = True


class TriggerIn(BaseModel):
    agent_id: str = ""
    event_type: str = ""
    filter: dict | None = None
    target_workflow_id: str = ""
    target_agent: str = ""
    target_capability: str = ""
    target_input: dict | None = None
    delegation_id: str = ""
    enabled: bool = True


from app.api.deps import get_conn


def _error(exc: AutonomyError) -> JSONResponse:
    return coded_error_response(exc)


def _owned_agent_id(conn, ref: str) -> str:
    """Agent handle the caller owns; unknown-or-foreign → denial.

    Handles (not crypto ids) are stored on schedules/triggers: stable
    across key rotation and readable in UIs.
    """
    from app import agents
    from app.auth import OwnerDenied, current_principal

    try:
        agent = agents.resolve_agent(conn, ref or "default")
    except agents.AgentError:
        raise OwnerDenied()
    if agent["owner_id"] != current_principal().owner_id:
        raise OwnerDenied()
    return agent["id"]


def _denied():
    from app.auth import OwnerDenied

    raise OwnerDenied()


def _not_found():
    return JSONResponse(
        status_code=404,
        content={"detail": "Not found.", "code": "NOT_FOUND"},
    )


@router.get("/schedules")
def list_schedules_route(conn: sqlite3.Connection = Depends(get_conn)):
    from app.auth import current_principal

    mine = current_principal().owner_id
    return {"schedules": [
        s for s in autonomy.list_schedules(conn)
        if _schedule_owner(conn, s) == mine
    ]}


def _schedule_owner(conn, schedule: dict) -> str:
    from app import agents

    try:
        return agents.resolve_agent(
            conn, schedule["agent_id"])["owner_id"]
    except agents.AgentError:
        return ""


@router.post("/schedules")
def create_schedule_route(
    body: ScheduleIn, conn: sqlite3.Connection = Depends(get_conn)
):
    from app.auth import OwnerDenied

    try:
        agent = _owned_agent_id(conn, body.agent_id or "default")
        return autonomy.create_schedule(
            conn,
            agent_id=agent,
            name=body.name,
            kind=body.kind,
            trigger=body.trigger,
            tzname=body.timezone,
            target_agent=body.target_agent,
            capability=body.capability,
            task_input=body.task_input,
            purpose=body.purpose,
            delegation_id=body.delegation_id,
            timeout_seconds=body.timeout_seconds,
            enabled=body.enabled,
        )
    except OwnerDenied:
        return _not_found()
    except AutonomyError as exc:
        return _error(exc)


def _owned_schedule(conn, schedule_id: str) -> dict:
    """Schedule the caller owns; otherwise 404."""
    from app.auth import OwnerDenied, current_principal

    try:
        schedule = autonomy.get_schedule(conn, schedule_id)
    except AutonomyError:
        raise OwnerDenied()
    if _schedule_owner(conn, schedule) != current_principal().owner_id:
        raise OwnerDenied()
    return schedule


@router.get("/schedules/{schedule_id}")
def get_schedule_route(schedule_id: str,
                       conn: sqlite3.Connection = Depends(get_conn)):
    try:
        return _owned_schedule(conn, schedule_id)
    except AutonomyError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.post("/schedules/{schedule_id}/enable")
def enable_schedule_route(schedule_id: str,
                          conn: sqlite3.Connection = Depends(get_conn)):
    try:
        _owned_schedule(conn, schedule_id)
        return autonomy.set_schedule_enabled(conn, schedule_id, True)
    except AutonomyError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.post("/schedules/{schedule_id}/disable")
def disable_schedule_route(schedule_id: str,
                           conn: sqlite3.Connection = Depends(get_conn)):
    try:
        _owned_schedule(conn, schedule_id)
        return autonomy.set_schedule_enabled(conn, schedule_id, False)
    except AutonomyError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.delete("/schedules/{schedule_id}")
def delete_schedule_route(schedule_id: str,
                          conn: sqlite3.Connection = Depends(get_conn)):
    try:
        _owned_schedule(conn, schedule_id)
        return {"id": schedule_id,
                "deleted": autonomy.delete_schedule(conn, schedule_id)}
    except AutonomyError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


def _owned_trigger(conn, trigger_id: str) -> dict:
    """Trigger the caller owns (via its agent); otherwise 404."""
    from app.auth import OwnerDenied

    try:
        trigger = autonomy.get_trigger(conn, trigger_id)
    except AutonomyError:
        raise OwnerDenied()
    if not _trigger_visible(conn, trigger):
        raise OwnerDenied()
    return trigger


def _trigger_visible(conn, trigger: dict) -> bool:
    from app import agents
    from app.auth import OwnerDenied, current_principal

    try:
        agent = agents.resolve_agent(conn, trigger["agent_id"])
        return agent["owner_id"] == current_principal().owner_id
    except (agents.AgentError, KeyError):
        return False
    except OwnerDenied:
        return False


@router.get("/triggers")
def list_triggers_route(conn: sqlite3.Connection = Depends(get_conn)):
    return {"triggers": [
        t for t in autonomy.list_triggers(conn) if _trigger_visible(conn, t)
    ]}


@router.post("/triggers")
def create_trigger_route(
    body: TriggerIn, conn: sqlite3.Connection = Depends(get_conn)
):
    try:
        agent = _owned_agent_id(conn, body.agent_id or "default")
        return autonomy.create_trigger(
            conn,
            agent_id=agent,
            event_type=body.event_type,
            target_workflow_id=body.target_workflow_id,
            target_agent=body.target_agent,
            target_capability=body.target_capability,
            target_input=body.target_input,
            delegation_id=body.delegation_id,
            event_filter=body.filter,
            enabled=body.enabled,
        )
    except OwnerDenied:
        return _not_found()
    except AutonomyError as exc:
        return _error(exc)


@router.post("/triggers/{trigger_id}/enable")
def enable_trigger_route(trigger_id: str,
                          conn: sqlite3.Connection = Depends(get_conn)):
    try:
        _owned_trigger(conn, trigger_id)
        return autonomy.set_trigger_enabled(conn, trigger_id, True)
    except AutonomyError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.post("/triggers/{trigger_id}/disable")
def disable_trigger_route(trigger_id: str,
                          conn: sqlite3.Connection = Depends(get_conn)):
    try:
        _owned_trigger(conn, trigger_id)
        return autonomy.set_trigger_enabled(conn, trigger_id, False)
    except AutonomyError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.delete("/triggers/{trigger_id}")
def delete_trigger_route(trigger_id: str,
                         conn: sqlite3.Connection = Depends(get_conn)):
    try:
        _owned_trigger(conn, trigger_id)
        return {"id": trigger_id,
                "deleted": autonomy.delete_trigger(conn, trigger_id)}
    except AutonomyError as exc:
        return _error(exc)
    except OwnerDenied:
        return _not_found()


@router.get("/events")
def list_events_route(limit: int = 50,
                      conn: sqlite3.Connection = Depends(get_conn)):
    from app import agents
    from app.auth import current_principal

    mine = {a["id"] for a in agents.list_agents(
        conn, owner_id=current_principal().owner_id)}
    rows = conn.execute(
        "SELECT id, event_type, payload_json, agent_id, depth,"
        " root_task_id, created_at FROM events ORDER BY created_at DESC"
        " LIMIT ?",
        (max(1, min(limit, 200)),),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        if item.get("agent_id") and item["agent_id"] not in mine:
            continue
        import json

        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except ValueError:
            item["payload"] = {}
        out.append(item)
    return {"events": out}


__all__ = ["router"]
