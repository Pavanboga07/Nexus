"""Minimal workflows: named step DAGs mapped onto real tasks.

Each step becomes an orchestrator task (same routing, delegation,
policy, approval, and execution rules — nothing duplicated here).
Independent steps in one dependency level run concurrently (separate
DB connections per step); a step whose dependency did not COMPLETE is
marked BLOCKED and never runs. A workflow with any non-completed
required step ends FAILED; cancellation propagates to live step tasks.

Remote-target steps dispatch (DISPATCHED) and converge later through
the normal A2A completion path; the workflow row reflects that until
its children close.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from app import orchestration, tasks
from app.errors import NexusError

logger = logging.getLogger(__name__)


class WorkflowError(NexusError):
    """Workflow failure with machine ``code`` + HTTP ``status``."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _levels(steps: list[dict]) -> list[list[dict]]:
    """Topological levels; raises on unknown deps or cycles."""
    by_key = {s["key"]: s for s in steps}
    for step in steps:
        for dep in step.get("depends_on", []):
            if dep not in by_key:
                raise WorkflowError(
                    "BAD_DEPENDENCY",
                    f"step {step['key']!r} depends on unknown {dep!r}.",
                    status=400,
                )
    remaining = {s["key"] for s in steps}
    done: set[str] = set()
    levels = []
    while remaining:
        ready = [by_key[k] for k in sorted(remaining)
                 if all(d in done for d in by_key[k].get("depends_on", []))]
        if not ready:
            raise WorkflowError(
                "CYCLE", "workflow steps contain a dependency cycle.",
                status=400,
            )
        levels.append(ready)
        done.update(s["key"] for s in ready)
        remaining -= {s["key"] for s in ready}
    return levels


def _validate_steps(steps: Any) -> list[dict]:
    if not isinstance(steps, list) or not steps:
        raise WorkflowError(
            "BAD_WORKFLOW", "workflow needs a non-empty steps list.",
            status=400)
    seen: set[str] = set()
    clean = []
    for raw in steps:
        if not isinstance(raw, dict):
            raise WorkflowError(
                "BAD_WORKFLOW", "each step must be an object.", status=400)
        key = str(raw.get("key") or "").strip()
        if not key or key in seen:
            raise WorkflowError(
                "BAD_WORKFLOW",
                "each step needs a unique non-empty key.", status=400)
        seen.add(key)
        clean.append({
            "key": key,
            "target_agent": str(raw.get("target_agent") or "").strip(),
            "capability": str(raw.get("capability") or "").strip(),
            "input": raw.get("input") if isinstance(
                raw.get("input"), dict) else {},
            "depends_on": [str(d) for d in raw.get("depends_on", []) or []],
            "delegation_id": str(raw.get("delegation_id") or "").strip(),
            "purpose": (str(raw.get("purpose") or "answer").strip()
                        or "answer"),
        })
    _levels(clean)
    return clean


def create_workflow(conn: sqlite3.Connection, *, name: str,
                    steps: list[dict], owner_id: str = "local",
                    requesting_agent: str = "owner") -> dict[str, Any]:
    """Persist a workflow owned by one owner (no cross-owner planting)."""
    clean = _validate_steps(steps)
    now = _now()
    flow_id = f"wfl_{uuid.uuid4().hex[:12]}"
    with conn:
        conn.execute(
            "INSERT INTO workflows (id, owner_id, name, status,"
            " created_at, updated_at) VALUES (?, ?, ?, 'PENDING', ?, ?)",
            (flow_id, owner_id, (name or "").strip()[:200], now, now),
        )
        for step in clean:
            conn.execute(
                "INSERT INTO workflow_steps (step_id, workflow_id,"
                " step_key, target_agent, capability, input_json,"
                " delegation_id, purpose, depends_on_json, status,"
                " task_id, result_json, error, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', '',"
                " '{}', '', ?, ?)",
                (f"stp_{uuid.uuid4().hex[:12]}", flow_id, step["key"],
                 step["target_agent"], step["capability"],
                 json.dumps(step["input"]), step["delegation_id"],
                 step["purpose"], json.dumps(step["depends_on"]),
                 now, now),
            )
    return get_workflow(conn, flow_id)


def get_workflow(conn: sqlite3.Connection,
                 flow_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, owner_id, name, status, created_at, updated_at"
        " FROM workflows WHERE id = ?",
        (flow_id,),
    ).fetchone()
    if row is None:
        raise WorkflowError("NOT_FOUND", f"unknown workflow {flow_id!r}.",
                            status=404)
    flow = dict(row)
    flow["steps"] = [
        dict(r) for r in conn.execute(
            "SELECT step_id, step_key, target_agent, capability,"
            " input_json, depends_on_json, status, task_id, result_json,"
            " error, created_at, updated_at FROM workflow_steps"
            " WHERE workflow_id = ? ORDER BY created_at ASC, step_key ASC",
            (flow_id,),
        ).fetchall()
    ]
    return flow


def list_workflows(conn: sqlite3.Connection,
                   owner_id: str = "local") -> list[dict[str, Any]]:
    return [
        dict(r) for r in conn.execute(
            "SELECT id, owner_id, name, status, created_at, updated_at"
            " FROM workflows WHERE owner_id = ? ORDER BY created_at DESC"
            " LIMIT 50",
            (owner_id,),
        ).fetchall()
    ]


def _set_step(conn, step_id: str, status: str, *,
              task_id: str | None = None, result: Any = None,
              error: str = "") -> None:
    now = _now()
    if task_id is not None:
        conn.execute(
            "UPDATE workflow_steps SET status = ?, task_id = ?,"
            " updated_at = ? WHERE step_id = ?",
            (status, task_id, now, step_id),
        )
    elif result is not None:
        conn.execute(
            "UPDATE workflow_steps SET status = ?, result_json = ?,"
            " updated_at = ? WHERE step_id = ?",
            (status, json.dumps(result), now, step_id),
        )
    elif error:
        conn.execute(
            "UPDATE workflow_steps SET status = ?, error = ?,"
            " updated_at = ? WHERE step_id = ?",
            (status, error[:2000], now, step_id),
        )
    else:
        conn.execute(
            "UPDATE workflow_steps SET status = ?, updated_at = ?"
            " WHERE step_id = ?",
            (status, now, step_id),
        )


def _set_flow(conn, flow_id: str, status: str) -> None:
    conn.execute(
        "UPDATE workflows SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), flow_id),
    )


async def _safe_run_step(conn_factory: Callable, flow_id: str,
                         step: dict, requesting_agent: str) -> dict:
    """Run one step; a step bug fails its step, never its siblings."""
    try:
        return await _run_step(conn_factory, flow_id, step,
                               requesting_agent)
    except Exception as exc:  # noqa: BLE001 - recorded on the step row
        conn = conn_factory()
        try:
            with conn:
                _set_step(conn, step["step_id"], "FAILED",
                          error=f"STEP_ERROR: {type(exc).__name__}: {exc}")
            return _step_row(conn, step["step_id"])
        finally:
            conn.close()


async def _run_step(conn_factory: Callable, flow_id: str, step: dict,
                    requesting_agent: str) -> dict:
    """Run one step to a settled step-state; returns the step row.

    Compare-and-swap claim (PENDING → RUNNING) makes concurrent
    runners safe: only the claim winner executes. Unmet dependencies
    BLOCK only on failed siblings; incomplete ones defer (stay
    PENDING) for a later run.
    """
    conn = conn_factory()
    try:
        with conn:
            claimed = conn.execute(
                "UPDATE workflow_steps SET status = 'RUNNING',"
                " updated_at = ? WHERE step_id = ? AND status = 'PENDING'",
                (_now(), step["step_id"]),
            ).rowcount
        if not claimed:
            return _step_row(conn, step["step_id"])
        with conn:
            # Dependency gate before any task exists: failed siblings
            # BLOCK, incomplete siblings defer (back to PENDING).
            failed_dep = None
            waiting_dep = False
            for dep_key in step.get("depends_on", []):
                dep = conn.execute(
                    "SELECT status FROM workflow_steps WHERE workflow_id = ?"
                    " AND step_key = ?",
                    (flow_id, dep_key),
                ).fetchone()
                if dep is None or dep["status"] in (
                        "FAILED", "BLOCKED", "CANCELLED"):
                    failed_dep = dep_key
                    break
                if dep["status"] != "COMPLETED":
                    waiting_dep = True
            if failed_dep is not None:
                _set_step(conn, step["step_id"], "BLOCKED",
                          error=f"dependency {failed_dep!r} did not complete")
                conn.commit()
                return _step_row(conn, step["step_id"])
            if waiting_dep:
                _set_step(conn, step["step_id"], "PENDING")
                conn.commit()
                return _step_row(conn, step["step_id"])
            task = tasks.create_task(
                conn,
                requesting_agent_id=requesting_agent,
                target_agent=step["target_agent"],
                capability=step["capability"],
                task_input=dict(step["input"] or {}),
                purpose=step.get("purpose") or "answer",
                delegation_id=step.get("delegation_id") or "",
            )
            _set_step(conn, step["step_id"], "RUNNING",
                      task_id=task["task_id"])
            conn.commit()
        final, _ = await orchestration.run_task(conn, task["task_id"])
        with conn:
            if final["status"] == "COMPLETED":
                _set_step(conn, step["step_id"], "COMPLETED",
                          result=final["output"])
            elif final["status"] == "WAITING_APPROVAL":
                _set_step(conn, step["step_id"], "WAITING",
                          task_id=final["task_id"])
            elif final["status"] == "DISPATCHED":
                _set_step(conn, step["step_id"], "DISPATCHED",
                          task_id=final["task_id"])
            else:
                _set_step(
                    conn, step["step_id"], "FAILED",
                    error=f"{final.get('error_code')}: "
                          f"{final.get('error_detail')}")
            conn.commit()
            return _step_row(conn, step["step_id"])
    finally:
        conn.close()


def _step_row(conn, step_id: str) -> dict:
    return dict(conn.execute(
        "SELECT step_id, step_key, target_agent, capability, status,"
        " task_id, result_json, error FROM workflow_steps"
        " WHERE step_id = ?", (step_id,)).fetchone())


async def run_workflow(conn_factory: Callable, flow_id: str, *,
                       requesting_agent: str = "owner") -> dict[str, Any]:
    """Run all steps level by level (concurrent within a level).

    Returns the workflow; remote-dispatched or approval-waiting steps
    leave it RUNNING to converge later through normal completion.
    RUNNING flows may be re-entered to pick up newly-pending steps
    (CAS claims make concurrent runners safe).
    """
    probe = conn_factory()
    try:
        flow = get_workflow(probe, flow_id)
    finally:
        probe.close()
    if flow["status"] not in ("PENDING", "PAUSED", "RUNNING"):
        raise WorkflowError(
            "BAD_STATE",
            f"workflow is {flow['status']}; only PENDING, PAUSED, or"
            " RUNNING workflows run.",
            status=409,
        )
    _write_flow_status(conn_factory, flow_id, "RUNNING")
    levels = _levels([{
        "key": s["step_key"],
        "depends_on": json.loads(s["depends_on_json"]),
    } for s in flow["steps"]])
    by_key = {}
    conn0 = conn_factory()
    try:
        for row in conn0.execute(
                "SELECT step_id, step_key, target_agent, capability,"
                " input_json, delegation_id, purpose, depends_on_json"
                " FROM workflow_steps"
                " WHERE workflow_id = ?", (flow_id,)).fetchall():
            item = dict(row)
            item["input"] = json.loads(item.pop("input_json"))
            item["depends_on"] = json.loads(item.pop("depends_on_json"))
            by_key[item["step_key"]] = item
    finally:
        conn0.close()
    for level in levels:
        probe = conn_factory()
        try:
            paused = get_workflow(probe, flow_id)["status"] == "PAUSED"
        finally:
            probe.close()
        if paused:
            closer = conn_factory()
            try:
                return get_workflow(closer, flow_id)
            finally:
                closer.close()
        results = await asyncio.gather(*[
            _safe_run_step(conn_factory, flow_id, by_key[s["key"]],
                           requesting_agent)
            for s in level
        ])
        if any(r["status"] in ("FAILED", "BLOCKED") for r in results):
            # Dependents of failures block at their own level.
            pass
    return _close_workflow(conn_factory, flow_id)


def _write_flow_status(conn_factory: Callable, flow_id: str,
                       status: str) -> None:
    conn = conn_factory()
    try:
        with conn:
            _set_flow(conn, flow_id, status)
    finally:
        conn.close()


def _close_workflow(conn_factory: Callable,
                    flow_id: str) -> dict[str, Any]:
    conn = conn_factory()
    try:
        return _close_workflow_factory(conn, flow_id)
    finally:
        conn.close()


def on_task_settled(conn: sqlite3.Connection, task_id: str) -> None:
    """Bridge terminal task transitions into workflow steps.

    Called whenever a task backing a step reaches a terminal state —
    inline, via A2A convergence, timeout sweep, or cancellation. Syncs
    the linked step and re-closes the workflow; no-ops when the task
    backs no step.
    """
    try:
        task = tasks.get_task(conn, task_id)
    except tasks.TaskError:
        return
    if task["status"] not in tasks.TERMINAL:
        return
    row = conn.execute(
        "SELECT step_id, workflow_id FROM workflow_steps"
        " WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    with conn:
        if task["status"] == "COMPLETED":
            _set_step(conn, row["step_id"], "COMPLETED",
                      result=task["output"])
        else:
            _set_step(conn, row["step_id"], "FAILED",
                      error=f"{task.get('error_code')}: "
                            f"{task.get('error_detail')}")
    _close_workflow_factory(conn, row["workflow_id"])


def _close_workflow_factory(conn: sqlite3.Connection,
                            flow_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT status FROM workflow_steps WHERE workflow_id = ?",
        (flow_id,)).fetchall()
    states = [r["status"] for r in rows]
    if all(s == "COMPLETED" for s in states):
        final = "COMPLETED"
    elif any(s in ("RUNNING", "WAITING", "DISPATCHED", "PENDING", "PAUSED")
             for s in states):
        # Work remains (in flight or deferred): still running, never
        # failed merely because convergence hasn't happened yet.
        final = "RUNNING"
    else:
        final = "FAILED"
    with conn:
        _set_flow(conn, flow_id, final)
    return get_workflow(conn, flow_id)


def pause_workflow(conn_factory: Callable, flow_id: str) -> dict[str, Any]:
    """Pause: running levels finish their current steps, but no NEW
    steps start. Resume continues from durable state."""
    conn = conn_factory()
    try:
        flow = get_workflow(conn, flow_id)
        if flow["status"] not in ("PENDING", "RUNNING"):
            raise WorkflowError(
                "BAD_STATE", f"workflow is {flow['status']}.", status=409)
        with conn:
            _set_flow(conn, flow_id, "PAUSED")
        return get_workflow(conn, flow_id)
    finally:
        conn.close()


def recover_workflows(conn_factory: Callable) -> list[dict[str, Any]]:
    """Restart recovery: reconcile crashed RUNNING workflows.

    Steps whose backing tasks already settled sync to them; steps
    still marked active are requeued to PENDING (their in-process run
    died with the crash — the old task rows stay as audit, and
    timeouts guard anything that lingers). Flows return to PENDING for
    an explicit operator resume; nothing auto-executes on boot.
    """
    conn = conn_factory()
    try:
        flows = [dict(r) for r in conn.execute(
            "SELECT id FROM workflows WHERE status = 'RUNNING'").fetchall()]
    finally:
        conn.close()
    recovered = []
    for flow in flows:
        conn = conn_factory()
        try:
            with conn:
                for step in get_workflow(conn, flow["id"])["steps"]:
                    if step["status"] in ("COMPLETED", "FAILED", "BLOCKED",
                                          "CANCELLED"):
                        continue
                    synced = False
                    if step["task_id"]:
                        try:
                            task = tasks.get_task(
                                conn, step["task_id"])
                        except tasks.TaskError:
                            task = None
                        if task is not None and task["status"] == "COMPLETED":
                            _set_step(conn, step["step_id"], "COMPLETED",
                                      result=task["output"])
                            synced = True
                        elif (task is not None
                                and task["status"] in ("FAILED", "CANCELLED")):
                            _set_step(
                                conn, step["step_id"], "FAILED",
                                error=f"{task.get('error_code')}: "
                                      f"{task.get('error_detail')}")
                            synced = True
                    if not synced:
                        _set_step(conn, step["step_id"], "PENDING")
                        conn.execute(
                            "UPDATE workflow_steps SET task_id = ''"
                            " WHERE step_id = ?", (step["step_id"],))
                _set_flow(conn, flow["id"], "PENDING")
            recovered.append(get_workflow(conn, flow["id"]))
        finally:
            conn.close()
    return recovered


def cancel_workflow(conn_factory: Callable, flow_id: str) -> dict[str, Any]:
    """Cancel live step tasks; blocked/pending steps go CANCELLED."""
    conn = conn_factory()
    try:
        flow = get_workflow(conn, flow_id)
        if flow["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            raise WorkflowError(
                "BAD_STATE", f"workflow is {flow['status']}.", status=409)
        with conn:
            for step in flow["steps"]:
                if step["status"] in ("COMPLETED", "FAILED", "BLOCKED",
                                      "CANCELLED"):
                    continue
                if step["task_id"]:
                    try:
                        tasks.cancel_task(conn, step["task_id"])
                    except tasks.TaskError:
                        logger.debug("best-effort cancel of step task %r"
                                     " dropped", step["task_id"],
                                     exc_info=True)
                _set_step(conn, step["step_id"], "CANCELLED")
            _set_flow(conn, flow_id, "CANCELLED")
        return get_workflow(conn, flow_id)
    finally:
        conn.close()


__all__ = [
    "WorkflowError",
    "cancel_workflow",
    "create_workflow",
    "get_workflow",
    "list_workflows",
    "on_task_settled",
    "pause_workflow",
    "recover_workflows",
    "run_workflow",
]
