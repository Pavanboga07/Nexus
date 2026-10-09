"""Durable tasks: explicit state machine, persisted, auditable.

States: PENDING → RESOLVING → AUTHORIZED → [WAITING_APPROVAL →]
DISPATCHED → RUNNING → COMPLETED. FAILED from anywhere non-terminal.
CANCELLED from anywhere non-terminal except RUNNING (no preemption —
a running step records CANCEL_REQUESTED instead of lying about it).

Every transition appends a ``task_events`` row. Terminal states are
absorbing: COMPLETED/FAILED/CANCELLED never leave except through an
explicit ``retry_task`` which mints a NEW task preserving lineage.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.errors import NexusError
from app.events import emit_best_effort

TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "APPROVAL_EXPIRED")

#: Allowed outgoing edges per state (besides * → FAILED/CANCELLED,
#: with RUNNING → CANCEL_REQUESTED instead of CANCELLED).
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PENDING": ("RESOLVING", "FAILED", "CANCELLED"),
    "RESOLVING": ("AUTHORIZED", "FAILED", "CANCELLED"),
    "AUTHORIZED": ("DISPATCHED", "WAITING_APPROVAL", "FAILED", "CANCELLED"),
    "DISPATCHED": ("RUNNING", "COMPLETED", "FAILED", "CANCELLED"),
    "RUNNING": ("COMPLETED", "FAILED", "CANCEL_REQUESTED"),
    "CANCEL_REQUESTED": ("COMPLETED", "FAILED", "CANCELLED"),
    "WAITING_APPROVAL": ("AUTHORIZED", "FAILED", "CANCELLED",
                         "APPROVAL_EXPIRED"),
    "COMPLETED": (),
    "FAILED": (),
    "CANCELLED": (),
    "APPROVAL_EXPIRED": (),
}

#: Failure codes that must never auto-retry (permanent, not transient).
NON_RETRYABLE = frozenset({
    "POLICY_DENIED", "DELEGATION_DENIED", "DELEGATION_REQUIRED",
    "NO_DELEGATION", "APPROVAL_REQUIRED", "APPROVAL_REJECTED",
    "CAPABILITY_NOT_FOUND", "CAPABILITY_NOT_OWNED", "CAPABILITY_DISABLED",
    "NO_TOOL", "UNKNOWN_TOOL", "AGENT_NOT_FOUND", "AGENT_INACTIVE",
    "AGENT_DISABLED", "AGENT_REVOKED", "NOT_FOUND", "BAD_REQUEST",
    "UNCORRELATED", "INVALID_RECIPIENT", "AMBIGUOUS",
})

MAX_RETRIES = 3
DEFAULT_TIMEOUT_SECONDS = 300
#: Budget: a parent may fan out this many children at most.
MAX_CHILDREN = 50


class TaskError(NexusError):
    """Task failure with machine ``code`` + HTTP ``status``."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deadline(seconds: int) -> str:
    seconds = max(1, int(seconds or DEFAULT_TIMEOUT_SECONDS))
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _event(conn: sqlite3.Connection, task_id: str, event: str,
           detail: str = "") -> None:
    conn.execute(
        "INSERT INTO task_events (event_id, task_id, event, detail,"
        " created_at) VALUES (?, ?, ?, ?, ?)",
        (_new_id("dev"), task_id, event, detail[:2000], _now()),
    )


_TASK_COLUMNS = (
    "task_id, owner_id, requesting_agent_id, target_agent_id,"
    " capability_id, requested_target, requested_capability, purpose,"
    " status, input_json, output_json, error_code,"
    " error_detail, correlation_id, parent_task_id, retry_of,"
    " retry_count, delegation_id, approval_id, idempotency_key,"
    " timeout_seconds, deadline_at, started_at, completed_at,"
    " created_at, updated_at, remote_task_id,"
    " origin, depth, root_task_id, trigger_id"
)


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    task = dict(row)
    for field in ("input_json", "output_json"):
        raw = task.get(field) or "{}"
        try:
            task[field.replace("_json", "")] = json.loads(raw)
        except ValueError:
            task[field.replace("_json", "")] = {}
    return task


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise TaskError("NOT_FOUND", f"unknown task {task_id!r}.",
                        status=404)
    return _row_to_task(row)


def list_tasks(conn: sqlite3.Connection, owner_id: str = "local",
               status: str | None = None,
               limit: int = 50) -> list[dict[str, Any]]:
    query = f"SELECT {_TASK_COLUMNS} FROM tasks WHERE owner_id = ?"
    params: tuple = (owner_id,)
    if status:
        query += " AND status = ?"
        params += (status,)
    query += " ORDER BY created_at DESC LIMIT ?"
    params += (max(1, min(limit, 200)),)
    return [_row_to_task(r) for r in conn.execute(query, params).fetchall()]


def get_task_by_correlation(conn: sqlite3.Connection,
                              correlation_id: str) -> dict[str, Any]:
    """Newest live task for a correlation; raises NOT_FOUND when none."""
    row = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE correlation_id = ?"
        " AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')"
        " ORDER BY created_at DESC LIMIT 1",
        (correlation_id,),
    ).fetchone()
    if row is None:
        raise TaskError("NOT_FOUND",
                        "no live task for this correlation.", status=404)
    return _row_to_task(row)


def task_children(conn: sqlite3.Connection,
                  task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE parent_task_id = ?"
        " ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def task_events(conn: sqlite3.Connection,
                task_id: str) -> list[dict[str, Any]]:
    get_task(conn, task_id)  # 404 on unknown
    rows = conn.execute(
        "SELECT event, detail, created_at FROM task_events"
        " WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_task(
    conn: sqlite3.Connection,
    *,
    owner_id: str = "local",
    requesting_agent_id: str = "",
    target_agent: str = "",
    capability: str = "",
    purpose: str = "answer",
    task_input: dict | None = None,
    parent_task_id: str = "",
    delegation_id: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    idempotency_key: str = "",
    origin: str = "user",
    depth: int = 0,
    root_task_id: str = "",
    trigger_id: str = "",
) -> dict[str, Any]:
    """Persist a PENDING task. Same non-empty idempotency key + same
    canonical params returns the existing task; same key with different
    params is a 409 (keys must not cross requests)."""
    try:
        clean_purpose = (purpose or "answer").strip() or "answer"
        params_blob = json.dumps({
            "target_agent": (target_agent or "").strip(),
            "capability": (capability or "").strip(),
            "input": task_input or {},
            "parent": (parent_task_id or "").strip(),
            "delegation": (delegation_id or "").strip(),
            "purpose": clean_purpose,
        }, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise TaskError("BAD_REQUEST",
                        f"task input not JSON-serializable: {exc}",
                        status=400) from exc
    if parent_task_id:
        kids = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?",
            ((parent_task_id or "").strip(),),
        ).fetchone()[0]
        if kids >= MAX_CHILDREN:
            raise TaskError(
                "BUDGET_EXCEEDED",
                f"parent already has {MAX_CHILDREN} children.",
                status=409,
            )
    key = (idempotency_key or "").strip()
    if key:
        dup = conn.execute(
            "SELECT task_id FROM tasks WHERE owner_id = ?"
            " AND idempotency_key = ?",
            (owner_id, key),
        ).fetchone()
        if dup is not None:
            existing = get_task(conn, dup["task_id"])
            if json.dumps({"target_agent": existing["requested_target"],
                           "capability": existing["requested_capability"],
                           "input": existing["input"],
                           "parent": existing["parent_task_id"],
                           "delegation": existing["delegation_id"],
                           "purpose": existing["purpose"]}, sort_keys=True
                           ) != params_blob:
                raise TaskError(
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency key already used with different params.",
                    status=409,
                )
            return existing
    now = _now()
    task_id = _new_id("tsk")
    correlation_id = f"corr_{uuid.uuid4().hex[:12]}"
    raw_target = (target_agent or "").strip()
    raw_capability = (capability or "").strip()
    clean_origin = (origin or "user").strip() or "user"
    clean_depth = max(0, int(depth or 0))
    parent = (parent_task_id or "").strip()
    row = {
        "task_id": task_id,
        "owner_id": owner_id,
        "requesting_agent_id": requesting_agent_id.strip(),
        "target_agent_id": raw_target,
        "capability_id": raw_capability,
        "requested_target": raw_target,
        "requested_capability": raw_capability,
        "purpose": clean_purpose,
        "status": "PENDING",
        "input_json": json.dumps(task_input or {}),
        "output_json": "{}",
        "error_code": "",
        "error_detail": "",
        "correlation_id": correlation_id,
        "parent_task_id": parent,
        "retry_of": "",
        "retry_count": 0,
        "delegation_id": (delegation_id or "").strip(),
        "approval_id": "",
        "idempotency_key": key,
        "timeout_seconds": max(
            1, int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)),
        "deadline_at": _deadline(timeout_seconds),
        "started_at": "",
        "completed_at": "",
        "created_at": now,
        "updated_at": now,
        "remote_task_id": "",
        "origin": clean_origin,
        "depth": clean_depth,
        "root_task_id": (root_task_id or "").strip() or task_id,
        "trigger_id": (trigger_id or "").strip(),
    }
    with conn:
        conn.execute(
            f"INSERT INTO tasks ({', '.join(row)})"
            f" VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        _event(conn, task_id, "TASK_CREATED")
    return get_task(conn, task_id)


def transition(conn: sqlite3.Connection, task_id: str, to: str,
               *, detail: str = "", commit: bool = True) -> dict[str, Any]:
    """Move a task along an allowed edge (or record terminal failure).

    ``to="FAILED"`` additionally requires error info via
    :func:`fail_task`; use that instead of transitioning directly.
    """
    task = get_task(conn, task_id)
    if task["status"] in TERMINAL:
        raise TaskError(
            "BAD_TRANSITION",
            f"task is terminal ({task['status']}); retry explicitly.",
            status=409,
        )
    allowed = TRANSITIONS.get(task["status"], ())
    if to not in allowed:
        raise TaskError(
            "BAD_TRANSITION",
            f"{task['status']} -> {to} is not a valid transition.",
            status=409,
        )
    now = _now()
    if commit:
        with conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?"
                " WHERE task_id = ?",
                (to, now, task_id),
            )
            _event(conn, task_id, f"TASK_{to}", detail)
            if to in ("RUNNING", "DISPATCHED") and not task["started_at"]:
                conn.execute(
                    "UPDATE tasks SET started_at = ? WHERE task_id = ?",
                    (now, task_id),
                )
            if to in TERMINAL:
                conn.execute(
                    "UPDATE tasks SET completed_at = ? WHERE task_id = ?",
                    (now, task_id),
                )
    else:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (to, now, task_id),
        )
        _event(conn, task_id, f"TASK_{to}", detail)
    return get_task(conn, task_id)


def fail_task(conn: sqlite3.Connection, task_id: str, code: str,
              detail: str = "") -> dict[str, Any]:
    """Move to FAILED with a persisted machine-readable reason."""
    task = get_task(conn, task_id)
    if task["status"] in TERMINAL:
        raise TaskError(
            "BAD_TRANSITION",
            f"task is terminal ({task['status']}); retry explicitly.",
            status=409,
        )
    now = _now()
    with conn:
        conn.execute(
            "UPDATE tasks SET status = 'FAILED', error_code = ?,"
            " error_detail = ?, updated_at = ?, completed_at = ?"
            " WHERE task_id = ?",
            (code, detail[:2000], now, now, task_id),
        )
        _event(conn, task_id, "TASK_FAILED", f"{code}: {detail[:500]}")
    _bridge_workflow_step(conn, task_id)
    _emit_task_event(conn, get_task(conn, task_id), "task.failed")
    return get_task(conn, task_id)


def complete_task(conn: sqlite3.Connection, task_id: str,
                  output: Any) -> dict[str, Any]:
    """Mark COMPLETED with a structured result (no NLP parsing)."""
    try:
        blob = json.dumps(output if output is not None else {})
    except (TypeError, ValueError) as exc:
        raise TaskError("BAD_REQUEST",
                        f"task output not JSON-serializable: {exc}",
                        status=400) from exc
    task = get_task(conn, task_id)
    if task["status"] in TERMINAL:
        raise TaskError(
            "BAD_TRANSITION",
            f"task is terminal ({task['status']}); retry explicitly.",
            status=409,
        )
    now = _now()
    with conn:
        conn.execute(
            "UPDATE tasks SET status = 'COMPLETED', output_json = ?,"
            " updated_at = ?, completed_at = ? WHERE task_id = ?",
            (blob, now, now, task_id),
        )
        _event(conn, task_id, "TASK_COMPLETED")
    _bridge_workflow_step(conn, task_id)
    _emit_task_event(conn, get_task(conn, task_id), "task.completed")
    return get_task(conn, task_id)


def complete_by_correlation(conn: sqlite3.Connection, correlation_id: str,
                            output: Any) -> dict[str, Any] | None:
    """Complete the live task awaiting this A2A correlation.

    Late responses for terminal tasks (e.g. cancelled while the answer
    was in flight) are IGNORED, never resurrecting them. Returns the
    task or None when nothing live matches.
    """
    row = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE correlation_id = ?"
        " AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')"
        " ORDER BY created_at DESC LIMIT 1",
        (correlation_id,),
    ).fetchone()
    if row is None:
        return None
    remote = ""
    if isinstance(output, dict):
        candidate = output.get("remote_task_id", "")
        if isinstance(candidate, str) and candidate.strip():
            remote = candidate.strip()
    if remote:
        with conn:
            conn.execute(
                "UPDATE tasks SET remote_task_id = ? WHERE task_id = ?",
                (remote, row["task_id"]),
            )
    return complete_task(conn, row["task_id"], output)


def cancel_task(conn: sqlite3.Connection, task_id: str,
                *, cascade: bool = True) -> dict[str, Any]:
    """Cancel a task (and live children). RUNNING cannot be preempted:
    it records CANCEL_REQUESTED — an honest marker, not a false claim."""
    task = get_task(conn, task_id)
    if task["status"] in TERMINAL:
        raise TaskError(
            "BAD_TRANSITION",
            f"task is terminal ({task['status']}).", status=409)
    if cascade:
        for child in task_children(conn, task_id):
            if child["status"] not in TERMINAL + ("CANCEL_REQUESTED",):
                cancel_task(conn, child["task_id"], cascade=True)
    if task["status"] == "RUNNING":
        out = transition(conn, task_id, "CANCEL_REQUESTED")
    else:
        out = transition(conn, task_id, "CANCELLED")
    _bridge_workflow_step(conn, task_id)
    return out


def retry_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Mint a fresh task preserving lineage. Permanent denials
    (policy, delegation, capability, agent, approval) are NOT retried —
    only transient failures are; capped at MAX_RETRIES."""
    task = get_task(conn, task_id)
    if task["status"] != "FAILED":
        raise TaskError(
            "BAD_TRANSITION", "only FAILED tasks can be retried.",
            status=409)
    if task["error_code"] in NON_RETRYABLE:
        raise TaskError(
            "NOT_RETRYABLE",
            f"{task['error_code']} is permanent; create a new task.",
            status=409)
    if task["retry_count"] >= MAX_RETRIES:
        raise TaskError(
            "RETRY_EXHAUSTED",
            f"already retried {MAX_RETRIES} times.", status=409)
    now = _now()
    new_id = _new_id("tsk")
    row = {
        "task_id": new_id,
        "owner_id": task["owner_id"],
        "requesting_agent_id": task["requesting_agent_id"],
        "target_agent_id": task["target_agent_id"],
        "capability_id": task["capability_id"],
        "requested_target": task["requested_target"],
        "requested_capability": task["requested_capability"],
        "purpose": task["purpose"],
        "status": "PENDING",
        "input_json": json.dumps(task["input"]),
        "output_json": "{}",
        "error_code": "",
        "error_detail": "",
        "correlation_id": task["correlation_id"],
        "parent_task_id": task["parent_task_id"],
        "retry_of": task["task_id"],
        "retry_count": task["retry_count"] + 1,
        "delegation_id": task["delegation_id"],
        "approval_id": "",
        "idempotency_key": "",
        "timeout_seconds": task["timeout_seconds"],
        "deadline_at": _deadline(task["timeout_seconds"]),
        "started_at": "",
        "completed_at": "",
        "created_at": now,
        "updated_at": now,
        "remote_task_id": "",
        "origin": task["origin"],
        "depth": task["depth"],
        "root_task_id": task["root_task_id"],
        "trigger_id": task["trigger_id"],
    }
    with conn:
        conn.execute(
            f"INSERT INTO tasks ({', '.join(row)})"
            f" VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        _event(conn, new_id, "TASK_RETRIED", f"retry_of={task_id}")
    return get_task(conn, new_id)


def renew_lease(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Refresh a task's deadline (e.g. resuming after owner approval)."""
    task = get_task(conn, task_id)
    with conn:
        conn.execute(
            "UPDATE tasks SET deadline_at = ?, updated_at = ?"
            " WHERE task_id = ?",
            (_deadline(task["timeout_seconds"]), _now(), task_id),
        )
        _event(conn, task_id, "TASK_LEASE_RENEWED")
    return get_task(conn, task_id)


def close_parent_if_done(conn: sqlite3.Connection,
                         parent_id: str) -> dict[str, Any] | None:
    """Complete a parent when all children are terminal.

    All children COMPLETED → parent COMPLETED with combined results
    keyed by task id. Any child FAILED/CANCELLED (rest terminal) →
    parent FAILED with CHILD_FAILED. Still-running children → None
    (parent waits). Never resurrects terminal parents.
    """
    try:
        parent = get_task(conn, parent_id)
    except TaskError:
        return None
    if parent["status"] in TERMINAL:
        return parent
    children = task_children(conn, parent_id)
    if not children:
        return None
    if any(c["status"] not in TERMINAL for c in children):
        return None
    if all(c["status"] == "COMPLETED" for c in children):
        combined = {c["task_id"]: c["output"] for c in children}
        return complete_task(conn, parent_id, {"children": combined})
    failed = [c["task_id"] for c in children
              if c["status"] != "COMPLETED"]
    return fail_task(conn, parent_id, "CHILD_FAILED",
                     f"children not completed: {failed[:5]}")


def sweep_timeouts(conn: sqlite3.Connection, now: str | None = None,
                   ) -> list[str]:
    """Fail tasks past deadline in DISPATCHED/RUNNING/WAITING_APPROVAL.

    Called opportunistically on task reads (no scheduler daemon in this
    phase); returns the failed task ids.
    """
    at = now or _now()
    rows = conn.execute(
        "SELECT task_id, status FROM tasks WHERE status IN"
        " ('DISPATCHED', 'RUNNING', 'WAITING_APPROVAL')"
        " AND deadline_at != '' AND deadline_at <= ?",
        (at,),
    ).fetchall()
    failed = []
    for row in rows:
        try:
            fail_task(conn, row["task_id"], "TIMEOUT",
                      f"{row['status']} exceeded its deadline")
            failed.append(row["task_id"])
        except TaskError:
            continue
    failed.extend(sweep_approval_expiry(conn, now=at))
    return failed


def sweep_approval_expiry(conn: sqlite3.Connection,
                          now: str | None = None) -> list[str]:
    """Expire WAITING_APPROVAL tasks whose bound card lapsed.

    A decided approval is consumed by advance; a still-pending card
    past ``expires_at`` moves the task to APPROVAL_EXPIRED (terminal) —
    approvals must never execute late and silently.
    """
    at = now or _now()
    rows = conn.execute(
        "SELECT t.task_id, a.expires_at, a.status FROM tasks t"
        " JOIN a2a_approvals a ON a.approval_id = t.approval_id"
        " WHERE t.status = 'WAITING_APPROVAL' AND t.approval_id != ''",
    ).fetchall()
    expired = []
    for row in rows:
        if row["status"] == "pending" and row["expires_at"] <= at:
            try:
                transition(conn, row["task_id"], "APPROVAL_EXPIRED",
                           detail="bound approval lapsed")
                expired.append(row["task_id"])
            except TaskError:
                continue
    return expired


def reconcile_tasks(conn: sqlite3.Connection,
                    now: str | None = None) -> dict[str, list[str]]:
    """Startup/crash reconciliation: bound every uncertain row.

    - Past-deadline DISPATCHED/RUNNING/WAITING_APPROVAL → FAILED(TIMEOUT)
    - Lapsed WAITING_APPROVAL cards → APPROVAL_EXPIRED
    - RUNNING rows are always stale (execution lived in-process, which
      is gone by definition when this runs) → FAILED(WORKER_GONE)
    Never marks anything completed; never reruns anything.
    """
    at = now or _now()
    failed = sweep_timeouts(conn, now=at)
    for row in conn.execute(
            "SELECT task_id FROM tasks WHERE status = 'RUNNING'").fetchall():
        try:
            fail_task(conn, row["task_id"], "WORKER_GONE",
                      "running at restart; execution did not survive it")
            failed.append(row["task_id"])
        except TaskError:
            continue
    return {"failed": failed}


def _bridge_workflow_step(conn: sqlite3.Connection, task_id: str) -> None:
    """Sync workflow steps when their backing task settles.

    WHY the import stays lazy (real cycle, not an accident):
    ``app.workflows`` imports ``app.tasks`` at module top-level (steps
    are backed by tasks), so ``app.tasks`` importing ``app.workflows``
    at top-level would be a hard circular import. The one-directional
    lazy edge here is the documented dodge.
    """
    from app import workflows

    workflows.on_task_settled(conn, task_id)


def _emit_task_event(conn: sqlite3.Connection, task: dict[str, Any],
                     event_type: str) -> None:
    """Best-effort domain event for trigger matching. Must never break
    the task path itself (events are observability, not control)."""
    emit_best_effort(
        conn, event_type,
        {"task_id": task["task_id"],
         "status": task.get("status", ""),
         "capability_id": task.get("capability_id", ""),
         "correlation_id": task.get("correlation_id", "")},
        agent_id=task.get("target_agent_id", ""),
        depth=int(task.get("depth", 0)),
        root_task_id=task.get("root_task_id", ""),
    )


def require_task_actor(conn: sqlite3.Connection, task_id: str,
                       actor_agent_id: str) -> dict[str, Any]:
    """Actor authorization for task operations.

    The operator (owner authority) always passes with ``actor="owner"``.
    An AGENT actor may only touch tasks where it is the requester or the
    resolved target — never another agent's tasks. Unknown actors fail.
    """
    task = get_task(conn, task_id)
    actor = (actor_agent_id or "").strip()
    if actor in ("", "owner"):
        return task
    if actor in (task["requesting_agent_id"], task["target_agent_id"]):
        return task
    raise TaskError(
        "FORBIDDEN",
        "agent is neither the requester nor the target of this task.",
        status=403,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "NON_RETRYABLE",
    "TERMINAL",
    "TRANSITIONS",
    "TaskError",
    "cancel_task",
    "close_parent_if_done",
    "complete_by_correlation",
    "complete_task",
    "create_task",
    "fail_task",
    "get_task",
    "get_task_by_correlation",
    "list_tasks",
    "renew_lease",
    "reconcile_tasks",
    "require_task_actor",
    "retry_task",
    "sweep_timeouts",
    "task_children",
    "task_events",
    "transition",
]
