"""Bounded autonomy: schedules, events, triggers, budgets.

Autonomy is a permission, never an assumption. Every autonomous run
carries identity (agent), authority (policy/delegation/approval),
bounds (budgets, depth, expiry), and audit. Nothing here bypasses the
execution gate — schedules and triggers create ordinary orchestrator
tasks with ``origin`` set, so all Phase 3 rules apply unchanged.

Safety rails (module constants, enforced, tested):
- MAX_TRIGGER_DEPTH (causal chain length)
- MAX_SCHEDULE_FANOUT (tasks per single tick per schedule)
- Cron parsing is minimal (minute hour dom month dow; *, */n,
  lists, ranges) with no new dependency.
- Duplicate fires are impossible by construction: each scheduled
  occurrence maps to one idempotency key, and replays return the
  existing task.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import agents, orchestration, tasks, workflows
from app.errors import NexusError

MAX_TRIGGER_DEPTH = 5
MAX_SCHEDULE_FANOUT = 10
AUTONOMY_LEVELS = ("disabled", "approval_required", "limited", "enabled")
TICK_GRACE_SECONDS = 3600


class AutonomyError(NexusError):
    """Autonomy failure with machine ``code`` + HTTP ``status``."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --- cron ----------------------------------------------------------------

def _parse_field(text: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field into matching values (supports *, */n,
    a,b-c, ranges, steps)."""
    values: set[int] = set()

    def add(value: int, until: int, step: int) -> None:
        values.update(range(value, min(until, hi) + 1, max(1, step)))

    for part in (text or "").strip().split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise AutonomyError(
                    "BAD_CRON", f"bad cron step {step_text!r}.",
                    status=400) from exc
            if step < 1:
                raise AutonomyError(
                    "BAD_CRON", "cron step must be >= 1.", status=400)
        if part in ("", "*"):
            add(lo, hi, step)
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise AutonomyError(
                    "BAD_CRON", f"bad cron range {part!r}.",
                    status=400) from exc
            if not (lo <= start <= end <= hi):
                raise AutonomyError(
                    "BAD_CRON", f"cron range {part!r} out of bounds.",
                    status=400)
            add(start, end, step)
        else:
            try:
                value = int(part)
            except ValueError as exc:
                raise AutonomyError(
                    "BAD_CRON", f"bad cron value {part!r}.",
                    status=400) from exc
            if not lo <= value <= hi:
                raise AutonomyError(
                    "BAD_CRON", f"cron value {value} out of bounds.",
                    status=400)
            add(value, value, step)
    if not values:
        raise AutonomyError("BAD_CRON", "empty cron field.", status=400)
    return values


def _parse_cron(expr: str) -> tuple[set[int], ...]:
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        raise AutonomyError(
            "BAD_CRON",
            "cron needs 5 fields: minute hour day month weekday.",
            status=400,
        )
    bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    return tuple(
        _parse_field(part, lo, hi)
        for part, (lo, hi) in zip(parts, bounds))


def next_cron_fire(expr: str, after: datetime,
                   tzname: str = "UTC") -> datetime:
    """Next minute-boundary fire strictly after ``after``."""
    try:
        tz = ZoneInfo(tzname or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AutonomyError(
            "BAD_TIMEZONE", f"unknown timezone {tzname!r}.",
            status=400) from exc
    minute, hour, dom, month, dow = _parse_cron(expr)
    # Python weekday (Mon=0) vs cron (Sun=0): convert at compare time.
    cursor = after.astimezone(tz).replace(second=0, microsecond=0)
    cursor += timedelta(minutes=1)
    for _ in range(525600 + 60 * 24 * 366):  # ~366 days of minutes, bounded
        cron_dow = (cursor.weekday() + 1) % 7
        if (cursor.minute in minute and cursor.hour in hour
                and cursor.day in dom and cursor.month in month
                and cron_dow in dow):
            return cursor
        cursor += timedelta(minutes=1)
    raise AutonomyError(
        "BAD_CRON", "no fire time within a year.", status=400)


def _as_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# --- schedules ------------------------------------------------------------

def _schedule_row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for field in ("task_input_json",):
        raw = out.pop(field, "{}") or "{}"
        try:
            out["task_input"] = json.loads(raw)
        except ValueError:
            out["task_input"] = {}
    out["enabled"] = bool(out.get("enabled", 1))
    return out


def create_schedule(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    name: str = "",
    kind: str = "cron",
    trigger: str = "",
    tzname: str = "UTC",
    target_agent: str = "",
    capability: str = "",
    task_input: dict | None = None,
    purpose: str = "answer",
    delegation_id: str = "",
    timeout_seconds: int = 300,
    enabled: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a schedule and compute its first fire time."""

    handle = (agent_id or "").strip()
    if not handle:
        raise AutonomyError(
            "BAD_SCHEDULE", "schedule needs an agent.", status=400)
    try:
        agent = agents.resolve_agent(conn, handle)
    except agents.AgentError as exc:
        raise AutonomyError(exc.code, str(exc), status=exc.status) from exc
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    kind = (kind or "cron").strip()
    if kind == "once":
        try:
            fire_at = datetime.fromisoformat((trigger or "").strip())
        except ValueError as exc:
            raise AutonomyError(
                "BAD_SCHEDULE",
                "once schedules need an ISO datetime trigger.",
                status=400) from exc
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=timezone.utc)
        next_run = fire_at
    elif kind == "cron":
        next_run = next_cron_fire(trigger, moment, tzname)
    else:
        raise AutonomyError(
            "BAD_SCHEDULE", "kind must be 'cron' or 'once'.", status=400)
    schedule_id = _new_id("sch")
    try:
        input_blob = json.dumps(task_input or {})
    except (TypeError, ValueError) as exc:
        raise AutonomyError(
            "BAD_SCHEDULE", f"task input not JSON: {exc}.",
            status=400) from exc
    with conn:
        conn.execute(
            "INSERT INTO schedules (id, owner_id, agent_id, name, kind,"
            " trigger, timezone, target_agent, capability,"
            " task_input_json, purpose, delegation_id, timeout_seconds,"
            " enabled, next_run_at, last_run_at, created_at, updated_at)"
            " VALUES (?, 'local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " '', ?, ?)",
            (schedule_id, agent["id"], (name or "").strip()[:200], kind,
             (trigger or "").strip(), (tzname or "UTC").strip() or "UTC",
             (target_agent or "").strip(), (capability or "").strip(),
             input_blob, (purpose or "answer").strip() or "answer",
             (delegation_id or "").strip(),
             max(60, int(timeout_seconds or 300)),
             1 if enabled else 0, _as_utc_iso(next_run),
             _as_utc_iso(moment), _as_utc_iso(moment)),
        )
    return get_schedule(conn, schedule_id)


def get_schedule(conn: sqlite3.Connection,
                 schedule_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, owner_id, agent_id, name, kind, trigger, timezone,"
        " target_agent, capability, task_input_json, purpose,"
        " delegation_id, timeout_seconds, enabled, next_run_at,"
        " last_run_at, created_at, updated_at FROM schedules"
        " WHERE id = ?",
        (schedule_id,),
    ).fetchone()
    if row is None:
        raise AutonomyError(
            "NOT_FOUND", f"unknown schedule {schedule_id!r}.",
            status=404)
    return _schedule_row(row)


def list_schedules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [_schedule_row(r) for r in conn.execute(
        "SELECT id, owner_id, agent_id, name, kind, trigger, timezone,"
        " target_agent, capability, task_input_json, purpose,"
        " delegation_id, timeout_seconds, enabled, next_run_at,"
        " last_run_at, created_at, updated_at FROM schedules"
        " ORDER BY created_at ASC").fetchall()]


def set_schedule_enabled(conn: sqlite3.Connection, schedule_id: str,
                         enabled: bool) -> dict[str, Any]:
    get_schedule(conn, schedule_id)
    with conn:
        conn.execute(
            "UPDATE schedules SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), schedule_id),
        )
    return get_schedule(conn, schedule_id)


def delete_schedule(conn: sqlite3.Connection, schedule_id: str) -> bool:
    get_schedule(conn, schedule_id)
    with conn:
        cur = conn.execute(
            "DELETE FROM schedules WHERE id = ?", (schedule_id,))
        return cur.rowcount > 0


def _agent_autonomy(conn: sqlite3.Connection, handle: str) -> str:
    row = conn.execute(
        "SELECT autonomy FROM agents WHERE id = ?", (handle,)
    ).fetchone()
    if row is None:
        return "disabled"
    level = str(row["autonomy"] or "limited")
    return level if level in AUTONOMY_LEVELS else "limited"


def _check_autonomy(conn: sqlite3.Connection, handle: str,
                    origin: str) -> tuple[bool, bool]:
    """Return (allowed, force_approval) for autonomous origins.

    Owner-initiated origins ("user", "chat") always pass: the operator
    is present. Schedules and triggers pass through the agent's
    lifecycle (must be active) AND autonomy level instead.
    """
    if origin in ("user", "chat"):
        return True, False
    row = conn.execute(
        "SELECT status, autonomy FROM agents WHERE id = ?", (handle,)
    ).fetchone()
    if row is None or row["status"] != "active":
        return False, False
    level = str(row["autonomy"] or "limited")
    if level not in AUTONOMY_LEVELS:
        level = "limited"
    if level == "disabled":
        return False, False
    if level == "approval_required":
        return True, True
    return True, False


async def scheduler_tick(conn: sqlite3.Connection, *,
                         now: datetime | None = None,
                         ) -> list[dict[str, Any]]:
    """Fire due schedules once each (idempotent across restarts).

    Each occurrence maps to one idempotency key, so a restart replay
    returns the existing task instead of duplicating work. Stale
    one-shot schedules fire once (catch-up) instead of storming.
    Returns the created/found task rows.
    """

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    fired = []
    due = conn.execute(
        "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at != ''"
        " AND next_run_at <= ? ORDER BY next_run_at ASC",
        (_as_utc_iso(moment),),
    ).fetchall()
    for raw in due[:MAX_SCHEDULE_FANOUT]:
        schedule = _schedule_row(raw)
        allowed, force_approval = _check_autonomy(
            conn, schedule["agent_id"], "schedule")
        if not allowed:
            continue
        fire_key = f"sched:{schedule['id']}:{schedule['next_run_at']}"
        _advance_schedule(conn, schedule, moment)

        existing = conn.execute(
            "SELECT task_id FROM tasks WHERE owner_id = 'local'"
            " AND idempotency_key = ?",
            (fire_key,),
        ).fetchone()
        if existing is not None:
            fired.append(tasks.get_task(conn, existing["task_id"]))
            continue
        task = tasks.create_task(
            conn,
            requesting_agent_id=schedule["agent_id"],
            target_agent=schedule["target_agent"] or schedule["agent_id"],
            capability=schedule["capability"],
            purpose="answer",
            task_input=dict(schedule["task_input"] or {}),
            delegation_id=schedule["delegation_id"],
            timeout_seconds=schedule["timeout_seconds"],
            idempotency_key=fire_key,
            origin="schedule",
        )
        with conn:
            conn.execute(
                "UPDATE schedules SET last_run_at = ? WHERE id = ?",
                (_as_utc_iso(moment), schedule["id"]),
            )
        if force_approval:
            # Approval-gated autonomy: park owner consent first and
            # stop — the task runs on advance, never straight through.
            # WHY lazy: app.a2a.service is the A2A facade with its own
            # lazy cycle edges; hoisting it here is out of scope for
            # this pass — its module owns those edges.
            from app.a2a import service as policy_service

            orchestration.resolve_task(conn, task["task_id"])
            card = policy_service.park_local_approval(
                conn,
                requester=(task["target_agent_id"]
                           or schedule["agent_id"]),
                action=task["capability_id"] or "message",
                question="Scheduled work "
                         f"{schedule['name'] or schedule['id']}",
                correlation_id=task["correlation_id"],
                purpose="answer",
            )
            with conn:
                conn.execute(
                    "UPDATE tasks SET approval_id = ? WHERE task_id = ?",
                    (card["approval_id"], task["task_id"]),
                )
            # resolve_task already moved PENDING → RESOLVING above.
            tasks.transition(conn, task["task_id"], "AUTHORIZED")
            fired.append(tasks.transition(
                conn, task["task_id"], "WAITING_APPROVAL",
                detail=card["approval_id"]))
            continue
        final, _ = await orchestration.run_task(conn, task["task_id"])
        fired.append(final)
    return fired


def _advance_schedule(conn, schedule: dict, moment: datetime) -> None:
    """Move next_run_at forward (cron) or park one-shots in the past."""
    if schedule["kind"] == "once":
        nxt: str = ""
    else:
        try:
            nxt = _as_utc_iso(next_cron_fire(
                schedule["trigger"], moment, schedule["timezone"]))
        except AutonomyError:
            nxt = ""
    with conn:
        conn.execute(
            "UPDATE schedules SET next_run_at = ?, updated_at = ?"
            " WHERE id = ?",
            (nxt, _as_utc_iso(moment), schedule["id"]),
        )


# --- events + triggers ----------------------------------------------------

def emit_event(conn: sqlite3.Connection, event_type: str,
               payload: dict | None = None, *, agent_id: str = "",
               depth: int = 0, root_task_id: str = "") -> dict[str, Any]:
    """Record a domain event for trigger matching (bounded payload)."""
    try:
        blob = json.dumps(payload or {})
    except (TypeError, ValueError):
        blob = "{}"
    event_id = _new_id("evt")
    with conn:
        conn.execute(
            "INSERT INTO events (id, event_type, payload_json, agent_id,"
            " depth, root_task_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, (event_type or "").strip(), blob,
             (agent_id or "").strip(), max(0, int(depth or 0)),
             (root_task_id or "").strip(), _now()),
        )
    row = conn.execute(
        "SELECT id, event_type, payload_json, agent_id, depth,"
        " root_task_id, created_at FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    out = dict(row)
    out["payload"] = json.loads(out.pop("payload_json"))
    return out


def _matches(payload: dict, filtr: dict) -> bool:
    if not filtr:
        return True
    for key, want in filtr.items():
        got = payload.get(key, object())
        if isinstance(want, list):
            if got not in want:
                return False
        elif got != want:
            return False
    return True


def create_trigger(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    event_type: str,
    target_workflow_id: str = "",
    target_agent: str = "",
    target_capability: str = "",
    target_input: dict | None = None,
    delegation_id: str = "",
    event_filter: dict | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Register an event trigger: one target (workflow XOR task)."""

    handle = (agent_id or "").strip()
    try:
        agents.resolve_agent(conn, handle)
    except agents.AgentError as exc:
        raise AutonomyError(exc.code, str(exc), status=exc.status) from exc
    if not (event_type or "").strip():
        raise AutonomyError(
            "BAD_TRIGGER", "event_type must not be empty.", status=400)
    has_flow = bool((target_workflow_id or "").strip())
    has_task = bool((target_agent or "").strip()
                    or (target_capability or "").strip())
    if has_flow == has_task:
        raise AutonomyError(
            "BAD_TRIGGER",
            "exactly one of target_workflow_id or"
            " target_agent/target_capability is required.",
            status=400,
        )
    try:
        filter_blob = json.dumps(event_filter or {})
        input_blob = json.dumps(target_input or {})
    except (TypeError, ValueError) as exc:
        raise AutonomyError(
            "BAD_TRIGGER", f"trigger JSON invalid: {exc}.",
            status=400) from exc
    trigger_id = _new_id("trg")
    with conn:
        conn.execute(
            "INSERT INTO triggers (id, owner_id, agent_id, event_type,"
            " filter_json, target_workflow_id, target_agent,"
            " target_capability, target_input_json, delegation_id,"
            " enabled, created_at, updated_at)"
            " VALUES (?, 'local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trigger_id, handle, (event_type or "").strip(), filter_blob,
             (target_workflow_id or "").strip(),
             (target_agent or "").strip(),
             (target_capability or "").strip(), input_blob,
             (delegation_id or "").strip(),
             1 if enabled else 0, _now(), _now()),
        )
    return get_trigger(conn, trigger_id)


def get_trigger(conn: sqlite3.Connection,
                trigger_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, owner_id, agent_id, event_type, filter_json,"
        " target_workflow_id, target_agent, target_capability,"
        " target_input_json, delegation_id, enabled, created_at,"
        " updated_at FROM triggers WHERE id = ?",
        (trigger_id,),
    ).fetchone()
    if row is None:
        raise AutonomyError(
            "NOT_FOUND", f"unknown trigger {trigger_id!r}.", status=404)
    out = dict(row)
    out["filter"] = json.loads(out.pop("filter_json") or "{}")
    out["target_input"] = json.loads(out.pop("target_input_json") or "{}")
    out["enabled"] = bool(out.get("enabled", 1))
    return out


def list_triggers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [get_trigger(conn, r["id"]) for r in conn.execute(
        "SELECT id FROM triggers ORDER BY created_at ASC").fetchall()]


def set_trigger_enabled(conn: sqlite3.Connection, trigger_id: str,
                        enabled: bool) -> dict[str, Any]:
    get_trigger(conn, trigger_id)
    with conn:
        conn.execute(
            "UPDATE triggers SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), trigger_id),
        )
    return get_trigger(conn, trigger_id)


def delete_trigger(conn: sqlite3.Connection, trigger_id: str) -> bool:
    get_trigger(conn, trigger_id)
    with conn:
        cur = conn.execute(
            "DELETE FROM triggers WHERE id = ?", (trigger_id,))
        return cur.rowcount > 0


async def process_events(conn: sqlite3.Connection, *,
                         limit: int = 100,
                         conn_factory=None) -> list[dict[str, Any]]:
    """Match pending events to enabled triggers and fire once each.

    Fired work re-enters through the orchestrator (same gates as manual
    work). Duplicate (trigger, event) pairs resolve to the existing
    task via idempotency keys — never duplicated.
    """

    fired: list[dict[str, Any]] = []
    events = conn.execute(
        "SELECT id, event_type, payload_json, agent_id, depth,"
        " root_task_id, created_at FROM events ORDER BY created_at ASC"
        " LIMIT ?",
        (max(1, min(limit, 500)),),
    ).fetchall()
    for raw in events:
        payload = json.loads(raw["payload_json"] or "{}")
        depth = int(raw["depth"] or 0)
        for row in conn.execute(
                "SELECT * FROM triggers WHERE enabled = 1"
                " AND event_type = ?",
                (raw["event_type"],)).fetchall():
            trigger = get_trigger(conn, row["id"])
            if not _matches(payload, trigger["filter"]):
                continue
            if depth >= MAX_TRIGGER_DEPTH:
                continue
            key = f"trig:{trigger['id']}:{raw['id']}"
            exists = conn.execute(
                "SELECT task_id FROM tasks WHERE owner_id = 'local'"
                " AND idempotency_key = ?",
                (key,),
            ).fetchone()
            if exists is not None:
                fired.append(
                    tasks.get_task(conn, exists["task_id"]))
                continue
            allowed, _ = _check_autonomy(
                conn, trigger["agent_id"], "trigger")
            if not allowed:
                continue
            if trigger["target_workflow_id"]:
                if conn_factory is None:
                    flow = workflows.get_workflow(
                        conn, trigger["target_workflow_id"])
                    fired.append({"workflow_id": flow["id"],
                                  "status": flow["status"],
                                  "note": "recorded; run explicitly"})
                    continue
                # The trigger's agent is the requesting context: remote
                # steps need a local signer, and audit needs an actor.
                done = await workflows.run_workflow(
                    conn_factory, trigger["target_workflow_id"],
                    requesting_agent=trigger["agent_id"])
                fired.append({"workflow_id": done["id"],
                              "status": done["status"]})
                continue
            task = tasks.create_task(
                conn,
                requesting_agent_id=trigger["agent_id"],
                target_agent=trigger["target_agent"],
                capability=trigger["target_capability"],
                task_input=dict(trigger["target_input"] or {}),
                delegation_id=trigger.get("delegation_id", ""),
                idempotency_key=key,
                origin="trigger",
                depth=depth + 1,
                root_task_id=raw["root_task_id"],
                trigger_id=trigger["id"],
            )
            final, _ = await orchestration.run_task(conn, task["task_id"])
            fired.append(final)
    return fired


__all__ = [
    "AUTONOMY_LEVELS",
    "MAX_SCHEDULE_FANOUT",
    "MAX_TRIGGER_DEPTH",
    "AutonomyError",
    "create_schedule",
    "create_trigger",
    "delete_schedule",
    "delete_trigger",
    "emit_event",
    "get_schedule",
    "get_trigger",
    "list_schedules",
    "list_triggers",
    "next_cron_fire",
    "process_events",
    "scheduler_tick",
    "set_schedule_enabled",
    "set_trigger_enabled",
]
