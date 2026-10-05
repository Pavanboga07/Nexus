"""Orchestrator: route, authorize, dispatch, and track multi-agent tasks.

Coordinates the Phase 2 primitives without reimplementing their rules:
identity/agents own keys, capabilities own skills, delegations own
cross-agent permission, policy owns ALLOW/ASK/DENY, approvals own
human consent, A2A owns transport. This module only moves tasks
through their lifecycle::

    submit (create → resolve → authorize → dispatch)
    advance (WAITING_APPROVAL → AUTHORIZED → dispatch)
    complete / fail / cancel / retry (via app.tasks)

Local targets execute inline through the execution gate; remote
(paired-peer) targets get a signed envelope whose delivery the caller
schedules (HTTP routes background it; tests drive it directly).
Groups (``input={"group": True}``) skip routing and complete when
their children close.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import tasks
from app.tasks import TaskError


def _is_group(task: dict[str, Any]) -> bool:
    return bool((task.get("input") or {}).get("group") is True)


def _local_agent(conn, ref: str) -> dict | None:
    from app import agents

    try:
        return agents.resolve_agent(conn, ref)
    except agents.AgentError:
        return None


def _paired_peer(conn, agent_id: str) -> dict | None:
    row = conn.execute(
        "SELECT agent_id, display_name FROM paired_peers WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def resolve_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """PENDING → RESOLVING: route target + capability, verify lifecycle.

    Explicit targets resolve locally first, then among paired peers.
    Capability-only requests resolve through discovery (exactly one
    explicit match or AMBIGUOUS with candidates — never a silent pick).
    Never falls back to the default agent.
    """
    from app import agents, capabilities, discovery

    task = tasks.get_task(conn, task_id)
    if task["status"] != "PENDING":
        raise TaskError(
            "BAD_TRANSITION",
            f"only PENDING tasks resolve (is {task['status']}).",
            status=409,
        )
    if _is_group(task):
        return tasks.transition(conn, task_id, "RESOLVING",
                                detail="group needs no routing")

    def _fail(code: str, detail: str) -> dict[str, Any]:
        return tasks.fail_task(conn, task_id, code, detail)

    want_target = (task["target_agent_id"] or "").strip()
    want_cap = (task["capability_id"] or "").strip()
    target_crypto = ""
    if want_target:
        local = _local_agent(conn, want_target)
        if local is not None:
            if local["status"] == "disabled":
                return _fail("AGENT_DISABLED",
                             f"target {local['id']!r} is disabled.")
            if local["status"] == "revoked":
                return _fail("AGENT_REVOKED",
                             f"target {local['id']!r} is revoked.")
            if local["status"] != "active":
                return _fail("AGENT_NOT_FOUND",
                             f"target {want_target!r} is not usable.")
            target_crypto = local["agent_id"]
        elif _paired_peer(conn, want_target) is not None:
            target_crypto = want_target
        else:
            return _fail("AGENT_NOT_FOUND",
                         f"unknown target agent {want_target!r}.")
    elif want_cap:
        found = discovery.resolve(conn, want_cap, fuzzy=False)
        if not found["matches"]:
            return _fail("CAPABILITY_NOT_FOUND",
                         f"no agent advertises {want_cap!r}.")
        if not found["explicit"]:
            return _fail("AMBIGUOUS",
                         "matches: " + ",".join(
                             m["id"] for m in found["matches"]))
        match = found["matches"][0]
        if match["kind"] == "agent":
            agent = _local_agent(conn, match["id"])
            if agent is None or agent["status"] != "active":
                return _fail("AGENT_NOT_FOUND",
                             "resolved agent is not usable.")
        target_crypto = match["id"]
    else:
        return _fail("NO_TARGET",
                     "task names no target agent or capability.")

    if want_cap:
        if not _capability_owned_by(conn, target_crypto, want_cap):
            return _fail("CAPABILITY_NOT_OWNED",
                         f"target does not own {want_cap!r}.")
        capability_id = want_cap
    else:
        capability_id = ""
    with conn:
        conn.execute(
            "UPDATE tasks SET target_agent_id = ?, capability_id = ?"
            " WHERE task_id = ?",
            (target_crypto, capability_id, task_id),
        )
    return tasks.transition(conn, task_id, "RESOLVING", detail=target_crypto)


def _capability_owned_by(conn, target_crypto: str, cap_id: str) -> bool:
    from app import capabilities, discovery

    try:
        cap = capabilities.get_capability(conn, cap_id)
        return cap["agent_id"] == target_crypto and cap["status"] == "active"
    except capabilities.CapabilityError:
        pass
    row = conn.execute(
        "SELECT card_json FROM paired_peers WHERE agent_id = ?",
        (target_crypto,),
    ).fetchone()
    if row is None:
        return False
    import json

    try:
        card = json.loads(row["card_json"])
    except (ValueError, TypeError):
        return False
    return cap_id in discovery._peer_capability_ids(card)


def _requesting_agent(conn, task: dict) -> tuple[str, str]:
    """Resolve the requesting principal: (kind, id).

    kind is "owner" (operator authority, needs no delegation) or
    "agent" (must hold a grant unless acting for itself).
    """
    from app import agents

    ref = (task["requesting_agent_id"] or "").strip()
    if ref in ("", "owner"):
        return "owner", "owner"
    agent = agents.resolve_agent(conn, ref)
    if agent["status"] != "active":
        raise TaskError(
            "AGENT_INACTIVE",
            f"requesting agent {agent['id']!r} is {agent['status']}.",
            status=403,
        )
    return "agent", agent["agent_id"]


def authorize_task(conn: sqlite3.Connection,
                   task_id: str) -> dict[str, Any]:
    """RESOLVING → AUTHORIZED | WAITING_APPROVAL | FAILED.

    Delegation (agent requesters acting across agents), then policy
    (DENY fails, ASK parks an approval card), then grant-demanded
    approval. Group tasks authorize trivially.
    """
    from app import delegations
    from app.a2a import service

    task = tasks.get_task(conn, task_id)
    if task["status"] != "RESOLVING":
        raise TaskError(
            "BAD_TRANSITION",
            f"only RESOLVING tasks authorize (is {task['status']}).",
            status=409,
        )
    if _is_group(task):
        return tasks.transition(conn, task_id, "AUTHORIZED",
                                detail="group")

    def _fail(code: str, detail: str) -> dict[str, Any]:
        return tasks.fail_task(conn, task_id, code, detail)

    kind, requester_id = _requesting_agent(conn, task)
    target_crypto = task["target_agent_id"]
    if kind == "agent" and requester_id != target_crypto:
        if not task["delegation_id"]:
            return _fail("DELEGATION_REQUIRED",
                         "cross-agent work needs a delegation grant.")
        try:
            grant = delegations.get_grant(conn, task["delegation_id"])
            delegations.verify_grant_signature(conn, {**grant,
                "signature": _stored_sig(conn, task["delegation_id"])})
            ok, reason = delegations.evaluate_grant(
                conn, grant, recipient_agent_id=target_crypto,
                capability_id=task["capability_id"],
                purpose=task["purpose"], task_id=task["correlation_id"])
        except delegations.DelegationError as exc:
            return _fail("DELEGATION_INVALID", str(exc))
        if not ok:
            return _fail("DELEGATION_INVALID", reason)

    if kind == "owner":
        requester_peer, grant_demands = "owner", False
    else:
        requester_peer = requester_id
        grant = None
        if kind == "agent" and requester_id != target_crypto:
            grant = delegations.get_grant(conn, task["delegation_id"])
        grant_demands = bool(
            (grant.get("constraints") or {}).get("require_approval")
        ) if grant else False

    action = task["capability_id"] or "message"
    agent_handle = _target_handle(conn, target_crypto)
    decision = service.evaluate_policy(
        conn,
        peer=requester_peer,
        data_category=str((task["input"] or {}).get(
            "data_category", "general")),
        purpose=task["purpose"] or "answer",
        action=action,
        agent=agent_handle,
    )
    if decision == "DENY":
        return _fail("POLICY_DENIED", "local policy denies this task.")
    if decision == "ASK" or grant_demands:
        # The card's "who" is the agent whose action needs consent
        # (never "owner": decide replies with a signed envelope, which
        # requires a valid agent id as recipient).
        card = service.park_local_approval(
            conn,
            requester=target_crypto,
            action=action,
            question=str((task["input"] or {}).get(
                "question",
                f"Approve {action} by {target_crypto} for task {task_id}")),
            correlation_id=task["correlation_id"],
            purpose=task["purpose"] or "answer",
        )
        with conn:
            conn.execute(
                "UPDATE tasks SET approval_id = ? WHERE task_id = ?",
                (card["approval_id"], task_id),
            )
        tasks.transition(conn, task_id, "AUTHORIZED",
                         detail="approval parked")
        return tasks.transition(conn, task_id, "WAITING_APPROVAL",
                                detail=card["approval_id"])
    return tasks.transition(conn, task_id, "AUTHORIZED", detail="allowed")


def _stored_sig(conn, grant_id: str) -> str:
    row = conn.execute(
        "SELECT signature FROM delegations WHERE id = ?", (grant_id,)
    ).fetchone()
    return str(row["signature"]) if row else ""


def _target_handle(conn, target_crypto: str) -> str | None:
    """Local handle for policy scoping; None for remote peers."""
    row = conn.execute(
        "SELECT id FROM agents WHERE agent_id = ?", (target_crypto,)
    ).fetchone()
    return str(row["id"]) if row is not None else None


async def dispatch_task(conn: sqlite3.Connection, task_id: str,
                        *, deliver=None,
                        secret: str | None = None
                        ) -> tuple[dict, dict | None]:
    """AUTHORIZED → run to completion (local) or DISPATCHED (remote).

    Local targets execute inline through the execution gate. Remote
    targets get a signed envelope; the caller delivers it (HTTP routes
    background it via ``app.dispatch``; tests drive it directly).
    Returns (task, envelope_or_None).
    """
    from app import agents
    from app.a2a import service
    from app.execution import ExecutionError, execute_capability

    task = tasks.get_task(conn, task_id)
    if task["status"] != "AUTHORIZED":
        raise TaskError(
            "BAD_TRANSITION",
            f"only AUTHORIZED tasks dispatch (is {task['status']}).",
            status=409,
        )
    if _is_group(task):
        raise TaskError("BAD_STATE",
                        "group tasks complete via children.", status=409)
    local = _local_agent(conn, task["target_agent_id"])
    if local is not None:
        tasks.transition(conn, task_id, "DISPATCHED", detail="local")
        tasks.transition(conn, task_id, "RUNNING", detail="local")
        try:
            out = await execute_capability(
                conn,
                acting_ref=local["id"],
                capability_id=task["capability_id"],
                args=dict(task["input"] or {}),
                requester_ref=task["requesting_agent_id"] or "owner",
                purpose=task["purpose"],
                delegation_id=task["delegation_id"] or None,
                task_id=task["correlation_id"],
            )
        except ExecutionError as exc:
            failed = tasks.fail_task(conn, task_id, exc.code, str(exc))
            tasks.close_parent_if_done(conn, task["parent_task_id"])
            return failed, None
        done = tasks.complete_task(conn, task_id, {
            "capability_id": task["capability_id"],
            "agent_id": local["agent_id"],
            "correlation_id": task["correlation_id"],
            "result": out.get("result"),
        })
        tasks.close_parent_if_done(conn, task["parent_task_id"])
        return done, None

    # Remote (paired peer) target: sign with the requesting agent.
    kind, requester_id = _requesting_agent(conn, task)
    if kind != "agent":
        failed = tasks.fail_task(
            conn, task_id, "NO_SIGNING_IDENTITY",
            "remote dispatch needs a local requesting agent to sign with.")
        return failed, None
    requester = agents.resolve_agent(conn, requester_id)
    priv, _ = agents.resolve_signing_key(
        conn, secret or _machine_secret(), requester["id"])
    task_input = dict(task["input"] or {})
    try:
        envelope = service.create_request(
            conn,
            signer_priv=priv,
            sender_id=requester_id,
            recipient_id=task["target_agent_id"],
            question=str(task_input.get("question", "")),
            action="answer",
            data_category=str(task_input.get("data_category", "general")),
            purpose=task["purpose"] or "answer",
            correlation_id=task["correlation_id"],
            payload_extra={
                "task_id": task["task_id"],
                "capability_id": task["capability_id"],
                "delegation_id": task["delegation_id"],
                "target_agent": task["target_agent_id"],
                "reply_to": task["parent_task_id"],
            },
        )
    except service.A2AError as exc:
        # Suspended/revoked/unknown peers fail the TASK, never the call.
        return tasks.fail_task(conn, task_id, exc.code, str(exc)), None
    tasks.transition(conn, task_id, "DISPATCHED",
                     detail=envelope["message_id"])
    if deliver is not None:
        await deliver(envelope)
    return tasks.get_task(conn, task_id), envelope


def _machine_secret() -> str:
    from app.machine_config import get_or_create_identity_secret

    return get_or_create_identity_secret()


async def advance_task(conn: sqlite3.Connection, task_id: str,
                       *, secret: str | None = None,
                       deliver=None) -> tuple[dict, dict | None]:
    """Resume WAITING_APPROVAL after owner action on the bound card."""
    task = tasks.get_task(conn, task_id)
    if task["status"] != "WAITING_APPROVAL":
        raise TaskError(
            "BAD_TRANSITION",
            f"only WAITING_APPROVAL tasks advance (is {task['status']}).",
            status=409,
        )
    row = conn.execute(
        "SELECT status FROM a2a_approvals WHERE approval_id = ?",
        (task["approval_id"],),
    ).fetchone()
    state = str(row["status"]) if row is not None else ""
    if state == "approved":
        tasks.transition(conn, task_id, "AUTHORIZED",
                         detail="approval accepted")
        tasks.renew_lease(conn, task_id)
        return await dispatch_task(conn, task_id, deliver=deliver,
                                   secret=secret)
    if state == "rejected":
        return tasks.fail_task(conn, task_id, "APPROVAL_REJECTED",
                               "owner rejected the task."), None
    if row is None:
        return tasks.fail_task(conn, task_id, "APPROVAL_REJECTED",
                               "bound approval is gone."), None
    return task, None


def reconcile_agent_tasks(conn: sqlite3.Connection,
                          agent_ref: str) -> dict[str, list[str]]:
    """Fail an agent's live work when it leaves ACTIVE.

    Called after disable/revoke. No auto-reassignment: moving work to
    another agent without explicit authorization could leak sensitive
    tasks. Returns the failed task ids per terminal cause.
    """
    from app import agents

    agent = agents.resolve_agent(conn, agent_ref)
    if agent["status"] == "active":
        return {"failed": []}
    code = "AGENT_REVOKED" if agent["status"] == "revoked" \
        else "AGENT_DISABLED"
    rows = conn.execute(
        "SELECT task_id FROM tasks WHERE (target_agent_id = ?"
        " OR target_agent_id = ?) AND status NOT IN ('COMPLETED',"
        " 'FAILED', 'CANCELLED', 'APPROVAL_EXPIRED')",
        (agent["agent_id"], agent["id"]),
    ).fetchall()
    failed = []
    for row in rows:
        try:
            tasks.fail_task(
                conn, row["task_id"], code,
                f"agent {agent['id']!r} is {agent['status']}")
            failed.append(row["task_id"])
        except tasks.TaskError:
            continue
    return {"failed": failed}


async def run_task(conn: sqlite3.Connection, task_id: str,
                   *, deliver=None,
                   secret: str | None = None) -> tuple[dict, dict | None]:
    """Full pipeline for one task: resolve → authorize → dispatch.

    Stops at FAILED or WAITING_APPROVAL with the task persisted (the
    FAILED task itself is returned, not raised); WAITING_APPROVAL
    resumes via :func:`advance_task`. ``secret`` unlocks agent keys
    sealed outside the machine secret (tests).
    """
    existing = tasks.get_task(conn, task_id)
    if existing["status"] != "PENDING":
        # Idempotent replay (or retry resume): already processed.
        return existing, None
    resolved = resolve_task(conn, task_id)
    if resolved["status"] != "RESOLVING":
        return resolved, None
    authorized = authorize_task(conn, task_id)
    if authorized["status"] != "AUTHORIZED":
        return authorized, None
    return await dispatch_task(conn, task_id, deliver=deliver,
                               secret=secret)


__all__ = [
    "advance_task",
    "authorize_task",
    "dispatch_task",
    "reconcile_agent_tasks",
    "resolve_task",
    "run_task",
]
