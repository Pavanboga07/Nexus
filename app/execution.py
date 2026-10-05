"""Capability execution gate: the ONE path from authorization to tools.

``execute_capability`` resolves, in order::

    authenticated/claimed acting agent
      → capability exists, active, owned by the acting agent
      → requester authorized (owner-self executes free; anyone else
        presents a live delegation grant)
      → local policy evaluated with the requester as peer
      → owner approval when policy ASKs or the grant demands it
      → bound tool runs
      → use audited

A remote agent never touches tools directly: it must arrive with a
delegation, survive policy, and — when required — owner approval. The
chat loop keeps its existing global executor (default agent); this
gate is what delegation and future per-agent loops must use.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


class ExecutionError(RuntimeError):
    """Execution refused/failed with machine ``code`` + HTTP ``status``."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status


def _is_self(requester: str, acting: dict[str, Any]) -> bool:
    return (requester or "").strip() in ("", "owner") or (
        requester.strip() in (acting["id"], acting["agent_id"])
    )


async def execute_capability(
    conn,
    *,
    acting_ref: str,
    capability_id: str,
    args: dict[str, Any] | None = None,
    requester_ref: str = "owner",
    purpose: str = "",
    delegation_id: str | None = None,
    task_id: str = "",
) -> dict[str, Any]:
    """Execute one capability for an authorized requester."""
    from app import agents, capabilities, delegations
    from app.a2a import service as policy_service
    from app.agent.tools import build_tools, execute_tool

    arguments = dict(args or {})
    acting = agents.resolve_agent(conn, acting_ref)
    if acting["status"] != "active":
        raise ExecutionError(
            "AGENT_INACTIVE",
            f"acting agent {acting['id']!r} is {acting['status']}.",
            status=403,
        )
    try:
        cap = capabilities.get_capability(conn, capability_id)
    except capabilities.CapabilityError as exc:
        raise ExecutionError(exc.code, str(exc), status=exc.status) from exc
    if cap["agent_id"] != acting["agent_id"]:
        raise ExecutionError(
            "WRONG_TOOL_OWNER",
            "capability is not owned by the acting agent.",
            status=403,
        )
    if cap["status"] != "active":
        raise ExecutionError(
            "CAPABILITY_DISABLED", "capability is not active.", status=409
        )
    if not cap.get("tool"):
        raise ExecutionError(
            "NO_TOOL",
            "capability binds no executable tool (descriptive only).",
            status=409,
        )
    if cap["tool"] not in build_tools():
        raise ExecutionError(
            "UNKNOWN_TOOL",
            f"bound tool {cap['tool']!r} is not installed.",
            status=500,
        )

    grant = None
    if not _is_self(requester_ref, acting):
        if not delegation_id:
            raise ExecutionError(
                "NO_DELEGATION",
                "cross-agent execution requires a delegation grant.",
                status=403,
            )
        try:
            grant = delegations.get_grant(conn, delegation_id)
        except delegations.DelegationError as exc:
            raise ExecutionError(
                exc.code, str(exc), status=exc.status) from exc
        delegations.verify_grant_signature(conn, {**grant,
            "signature": _grant_signature(conn, delegation_id)})
        ok, reason = delegations.evaluate_grant(
            conn, grant,
            recipient_agent_id=acting["agent_id"],
            capability_id=capability_id,
            purpose=purpose,
            task_id=task_id,
        )
        if not ok:
            _audit_use(conn, delegation_id, "denied", reason)
            raise ExecutionError("DELEGATION_DENIED", reason, status=403)
        # Approval demanded either by policy-ASK below or by the grant
        # itself; the unified check after policy evaluation covers both.
        grant_demands = bool(
            (grant.get("constraints") or {}).get("require_approval")
        )
        requester_peer = grant["issuer_agent_id"]
    else:
        requester_peer = "owner"
        grant_demands = False

    decision = policy_service.evaluate_policy(
        conn,
        peer=requester_peer,
        data_category=str(arguments.get("data_category", "general")),
        purpose=purpose or "answer",
        action=capability_id,
        agent=acting["id"],
    )
    if decision == "DENY":
        raise ExecutionError(
            "POLICY_DENY", "local policy denies this execution.",
            status=403,
        )
    if decision == "ASK" or grant_demands:
        bound_task = (task_id or (grant.get("task_id", "") if grant else ""))
        ok, reason = delegations.check_approval_binding(
            conn,
            {"constraints": {"require_approval": True}},
            bound_task,
        )
        if not ok:
            if grant is not None:
                _audit_use(conn, grant["id"], "denied", reason)
            raise ExecutionError("APPROVAL_REQUIRED", reason, status=403)

    try:
        raw = await execute_tool(build_tools(), cap["tool"], arguments)
    except Exception as exc:
        raise ExecutionError(
            "TOOL_FAILED", f"tool {cap['tool']!r} failed: {exc}",
            status=502,
        ) from exc
    try:
        result = json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
    except ValueError:
        result = raw
    if grant is not None:
        _audit_use(conn, grant["id"], "used", f"task={task_id or '-'}")
    return {
        "capability_id": capability_id,
        "tool": cap["tool"],
        "agent_id": acting["agent_id"],
        "result": result,
    }


def _grant_signature(conn, grant_id: str) -> str:
    row = conn.execute(
        "SELECT signature FROM delegations WHERE id = ?", (grant_id,)
    ).fetchone()
    return str(row["signature"]) if row else ""


def _audit_use(conn, grant_id: str, action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO delegation_events (event_id, delegation_id, action,"
        " detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (f"dev_{uuid.uuid4().hex[:12]}", grant_id, action, detail,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


__all__ = ["ExecutionError", "execute_capability"]
