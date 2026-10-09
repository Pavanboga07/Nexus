"""Ask/approve/answer loop (V4).

Five v0.3 types + error, frozen. Flow: ``create_request`` signs an
ask; ``receive_envelope`` verifies (pinned peer key only), enforces
expiry, rejects replays via the ``a2a_messages`` UNIQUE constraint
(never memory), and routes: ``request`` consults the policy engine,
``approval_request`` always parks an inline-approvable card carrying
who/what/why/expiry, ``approve``/``reject`` resolve, ``response`` is
stored as the answer, unknown types get a signed ``error``.

Policy (minimal engine, fail-closed from day one): rules are
user-created ALLOWs with ``*`` wildcards; no matching rule -> ASK
(park); ``SENSITIVE_CATEGORIES`` -> DENY even when a rule matches.
Specificity = count of non-wildcard fields; the most specific matching
rule wins (only ALLOW rules exist, so any match allows).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from app.a2a import envelope as app_envelope
from relay import envelope as relay_envelope

REQUEST_TYPES = ("request", "approval_request")
RESOLUTION_TYPES = ("approve", "reject")

#: Transport TTL (seconds) for delegation envelopes — the request and
#: its response. A task round-trip outlives the 300s envelope default,
#: and an offline peer must still receive the delegation when it
#: reconnects (audit §4 F6: separate the transport TTL from the
#: envelope TTL instead of letting queued delegations die at 5 min).
DELEGATION_TTL_SECONDS = 24 * 3600

# Minimal sensitive set (fail-closed DENY). Deliberately small: v1 has
# no classifier, so only obviously-secret categories deny outright.
# Everything else without a rule parks for a human (ASK).
SENSITIVE_CATEGORIES = frozenset({"credentials", "secret", "private_keys"})


class A2AError(ValueError):
    """Loop failure with machine ``code`` and optional HTTP ``status``."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(
        relay_envelope.TIMESTAMP_FORMAT
    )


def _now_iso(now: datetime | str | None) -> str:
    if now is None:
        return _iso(_utcnow())
    if isinstance(now, datetime):
        return _iso(now)
    return now


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# --- policy ---------------------------------------------------------------

def evaluate_policy(
    conn: sqlite3.Connection,
    *,
    peer: str,
    data_category: str,
    purpose: str,
    action: str,
    agent: str | None = None,
) -> str:
    """ALLOW when a rule matches (most specific wins), DENY when the
    category is sensitive, ASK otherwise (fail-closed default).

    ``agent`` scopes evaluation to one local agent: rules with
    ``agent_id`` ``'*'`` apply to every agent, rules naming an agent
    apply only to it. ``None`` (e.g. the identity-level A2A receive
    path) sees global rules only, preserving legacy behavior exactly.
    Sensitive-category DENY ignores rules entirely, whatever the agent.
    """
    if data_category in SENSITIVE_CATEGORIES:
        return "DENY"
    best: int | None = None
    for row in conn.execute(
        "SELECT peer, data_category, purpose, action, effect, agent_id "
        "FROM policy_rules WHERE effect = 'ALLOW'"
    ).fetchall():
        rule_agent = row["agent_id"] if "agent_id" in row.keys() else "*"
        if agent is None:
            if rule_agent != "*":
                continue
        elif rule_agent != "*" and rule_agent != agent:
            continue
        fields = (
            (row["peer"], peer),
            (row["data_category"], data_category),
            (row["purpose"], purpose),
            (row["action"], action),
        )
        if any(rule != "*" and rule != got for rule, got in fields):
            continue
        score = sum(1 for rule, _ in fields if rule != "*")
        if rule_agent != "*":
            score += 1
        if best is None or score > best:
            best = score
    return "ALLOW" if best is not None else "ASK"


def create_rule(
    conn: sqlite3.Connection,
    *,
    peer: str = "*",
    data_category: str = "*",
    purpose: str = "*",
    action: str = "*",
    effect: str = "ALLOW",
    agent: str | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Store a user-created ALLOW rule (the only effect in v1).

    ``agent`` scopes the rule to one local agent; ``'*'`` (default)
    keeps it global. Empty agent names are rejected fail-closed.
    """
    if effect != "ALLOW":
        raise A2AError(
            "INVALID_RULE",
            "v1 policy rules are ALLOW-only (DENY is reserved for "
            "sensitive categories).",
            status=400,
        )
    if agent is None:
        owner = "*"
    else:
        owner = agent.strip()
        if not owner:
            raise A2AError(
                "INVALID_RULE", "agent must not be empty.", status=400
            )
    rule_id = _new_id("rule")
    created_at = _now_iso(now)
    with conn:
        conn.execute(
            "INSERT INTO policy_rules "
            "(rule_id, peer, data_category, purpose, action, effect, "
            "agent_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rule_id, peer, data_category, purpose, action, effect,
             owner, created_at),
        )
    return {
        "rule_id": rule_id,
        "peer": peer,
        "data_category": data_category,
        "purpose": purpose,
        "action": action,
        "effect": effect,
        "agent_id": owner,
        "created_at": created_at,
    }


def list_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT rule_id, peer, data_category, purpose, action, effect, "
        "agent_id, created_at FROM policy_rules ORDER BY created_at ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def delete_rule(conn: sqlite3.Connection, rule_id: str) -> bool:
    with conn:
        cursor = conn.execute(
            "DELETE FROM policy_rules WHERE rule_id = ?", (rule_id,)
        )
    return cursor.rowcount > 0

# --- store helpers ----------------------------------------------------------

def _pinned_peer(conn: sqlite3.Connection, agent_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT agent_id, public_key, trust_state FROM paired_peers"
        " WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        raise A2AError(
            "NOT_PAIRED",
            "unknown peer: pair before exchanging messages.",
            status=404,
        )
    state = row["trust_state"] if "trust_state" in row.keys() else "TRUSTED"
    if state == "SUSPENDED":
        raise A2AError(
            "PEER_SUSPENDED",
            "peer is suspended: resume trust before exchanging messages.",
            status=403,
        )
    if state == "REVOKED":
        raise A2AError(
            "PEER_REVOKED",
            "peer trust was revoked: re-pair to exchange messages.",
            status=403,
        )
    return row


def _require_known_recipient(conn: sqlite3.Connection, agent_id: str) -> None:
    """Outbound recipients: a paired peer or a local agent. Anything
    else fails closed before anything is signed or stored. Trust-state
    rejections (suspended/revoked) propagate untouched — only a truly
    unknown peer falls through to the local-agent check."""
    try:
        _pinned_peer(conn, agent_id)
        return
    except A2AError as exc:
        if exc.code != "NOT_PAIRED":
            raise
    if _local_agent_key(conn, agent_id) is None:
        raise A2AError(
            "NOT_PAIRED",
            "unknown peer: pair before exchanging messages.",
            status=404,
        )


def _local_agent_key(conn: sqlite3.Connection, agent_id: str) -> str | None:
    """Active local agent's public key, or None (same-owner delivery)."""
    row = conn.execute(
        "SELECT i.public_key FROM agent_identities i"
        " JOIN agents a ON a.agent_id = i.agent_id"
        " WHERE i.agent_id = ? AND i.status = 'active'"
        " AND a.status = 'active'"
        " ORDER BY i.version DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return str(row["public_key"]) if row is not None else None


def _sender_public_key(conn: sqlite3.Connection, sender_id: str) -> str:
    """Verification key for an inbound sender: pinned peer first, then
    an active local agent (same-process multi-agent delivery). Unknown
    senders still fail closed via NOT_PAIRED."""
    try:
        return _pinned_peer(conn, sender_id)["public_key"]
    except A2AError as exc:
        if exc.code != "NOT_PAIRED":
            raise
    key = _local_agent_key(conn, sender_id)
    if key is None:
        raise A2AError(
            "NOT_PAIRED",
            "unknown peer: pair before exchanging messages.",
            status=404,
        )
    return key


def _handle_control_action(conn: sqlite3.Connection, live,
                           payload: dict, at: str,
                           ) -> dict[str, Any] | None:
    """Control traffic (cancel/status) bypasses approval parking.

    Returns an outcome dict when the action was control traffic (even
    when ignored), None for ordinary requests. Authorization: the
    sender must be a party of the referenced live task — strangers
    cannot cancel or annotate others' work.
    """
    from app import tasks as task_tracker

    action = str(payload.get("action", ""))
    if action not in ("task_cancel", "task_status"):
        return None
    try:
        task = task_tracker.get_task_by_correlation(
            conn, live.correlation_id)
    except task_tracker.TaskError:
        return {"outcome": "ignored", "approval": None, "reply": None}
    parties = {task["requesting_agent_id"], task["target_agent_id"]}
    if live.sender not in parties:
        return {"outcome": "ignored", "approval": None, "reply": None}
    if action == "task_cancel":
        if task["status"] in task_tracker.TERMINAL:
            return {"outcome": "ignored", "approval": None, "reply": None}
        if task["status"] == "RUNNING":
            task_tracker.transition(conn, task["task_id"],
                                    "CANCEL_REQUESTED")
            return {"outcome": "cancel_requested", "approval": None,
                    "reply": None}
        task_tracker.cancel_task(conn, task["task_id"], cascade=True)
        return {"outcome": "cancelled", "approval": None, "reply": None}
    # task_status: advisory remote progress, recorded as an event.
    status = str(payload.get("remote_status", "") or "")[:64]
    detail = str(payload.get("detail", "") or "")[:500]
    with conn:
        # OR IGNORE: duplicate deliveries of the same status must not
        # fail the ingest (transport retries are expected).
        conn.execute(
            "INSERT OR IGNORE INTO task_events (event_id, task_id, event,"
            " detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"dev_{live.message_id[:12]}", task["task_id"],
             f"TASK_REMOTE_{status or 'UNKNOWN'}", detail, at),
        )
    return {"outcome": "noted", "approval": None, "reply": None}


def _is_local_recipient(conn: sqlite3.Connection, recipient: str,
                        local_id: str) -> bool:
    """Envelopes may address the identity or any local agent directly."""
    if recipient == local_id:
        return True
    return _local_agent_key(conn, recipient) is not None


def _store(
    conn: sqlite3.Connection,
    envelope: dict[str, Any],
    status: str,
    now: str,
) -> None:
    """Persist one envelope; duplicate message_id -> REPLAY.

    The PRIMARY KEY constraint is the replay protection: no in-memory
    seen-sets anywhere on this path.
    """
    try:
        with conn:
            conn.execute(
                "INSERT INTO a2a_messages "
                "(message_id, correlation_id, sender, recipient, "
                "message_type, envelope_json, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    envelope["message_id"],
                    envelope["correlation_id"],
                    envelope["sender"],
                    envelope["recipient"],
                    envelope["message_type"],
                    json.dumps(envelope),
                    status,
                    now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise A2AError(
            "REPLAY",
            f"message_id {envelope['message_id']!r} already seen.",
            status=409,
        ) from exc


def _park_approval(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    correlation_id: str,
    requester: str,
    payload: dict[str, Any],
    expires_at: str,
    now: str,
) -> dict[str, Any]:
    """Park an inline-approvable card: who/what/why/expiry."""
    approval_id = _new_id("apr")
    card = {
        "approval_id": approval_id,
        "message_id": message_id,
        "correlation_id": correlation_id,
        "requester": requester,
        "action": str(payload.get("action", "answer")),
        "data_category": str(payload.get("data_category", "general")),
        "purpose": str(payload.get("purpose", "answer")),
        "question": str(payload.get("question", "")),
        "expires_at": expires_at,
        "status": "pending",
        "decided_at": "",
        "created_at": now,
    }
    try:
        with conn:
            conn.execute(
                "INSERT INTO a2a_approvals "
                "(approval_id, message_id, correlation_id, requester, "
                "action, data_category, purpose, question, expires_at, "
                "status, decided_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id, message_id, correlation_id, requester,
                    card["action"], card["data_category"], card["purpose"],
                    card["question"], expires_at, "pending", "", now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise A2AError(
            "REPLAY",
            f"message_id {message_id!r} already parked.",
            status=409,
        ) from exc
    return card


# --- outbound ---------------------------------------------------------------

def create_request(
    conn: sqlite3.Connection,
    *,
    signer_priv,
    sender_id: str,
    recipient_id: str,
    question: str = "",
    action: str = "answer",
    data_category: str = "general",
    purpose: str = "answer",
    message_type: str = "request",
    timestamp: datetime | str | None = None,
    expires_at: datetime | str | None = None,
    message_id: str | None = None,
    correlation_id: str | None = None,
    payload_extra: dict[str, Any] | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Sign an ask to a paired peer and store it as sent.

    ``payload_extra`` carries optional delegation fields
    (``capability_id``, ``delegation_id``, ``target_agent``,
    ``reply_to``) inside the signed payload — same v0.3 envelope,
    no protocol break.

    ``ttl_seconds`` overrides the envelope expiry; when None, task
    delegations (``capability_id``/``delegation_id`` in
    ``payload_extra``) get the 24h transport TTL
    (``DELEGATION_TTL_SECONDS``) while plain asks use the envelope
    default (``NEXUS_ENVELOPE_TTL_SECONDS``, 300s).
    """
    if message_type not in REQUEST_TYPES:
        raise A2AError(
            "INVALID_TYPE",
            f"ask types are {list(REQUEST_TYPES)}.",
            status=400,
        )
    _require_known_recipient(conn, recipient_id)
    extra = dict(payload_extra or {})
    try:
        json.dumps(extra)
    except (TypeError, ValueError) as exc:
        raise A2AError(
            "BAD_PAYLOAD", f"payload_extra not JSON-serializable: {exc}",
            status=400,
        ) from exc
    if ttl_seconds is None and (
        str(extra.get("capability_id") or "").strip()
        or str(extra.get("delegation_id") or "").strip()
    ):
        ttl_seconds = DELEGATION_TTL_SECONDS
    unsigned = app_envelope.new_envelope(
        sender=sender_id,
        recipient=recipient_id,
        message_type=message_type,
        payload={
            "action": action,
            "question": question,
            "data_category": data_category,
            "purpose": purpose,
            **extra,
        },
        timestamp=_now_iso(timestamp) if timestamp is not None else None,
        expires_at=_now_iso(expires_at) if expires_at is not None else None,
        message_id=message_id,
        correlation_id=correlation_id,
        ttl_seconds=ttl_seconds,
    )
    signed = app_envelope.sign(unsigned, signer_priv)
    _store(conn, signed, "sent", _now_iso(timestamp))
    return signed


def send_response(
    conn: sqlite3.Connection,
    *,
    signer_priv,
    sender_id: str,
    recipient_id: str,
    correlation_id: str,
    answer: str,
    timestamp: datetime | str | None = None,
    expires_at: datetime | str | None = None,
    message_id: str | None = None,
    remote_task_id: str = "",
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Sign an answer to a paired peer (post-approval) and store it.

    ``remote_task_id`` optionally names the responder's own task, so
    the originator can correlate origin_task ↔ remote_task. Responses
    carrying one are delegation round-trips and get the 24h transport
    TTL (``DELEGATION_TTL_SECONDS``) unless ``ttl_seconds`` overrides.
    """
    _require_known_recipient(conn, recipient_id)
    payload: dict[str, Any] = {"action": "answer", "answer": answer}
    if (remote_task_id or "").strip():
        payload["remote_task_id"] = remote_task_id.strip()
    if ttl_seconds is None and payload.get("remote_task_id"):
        ttl_seconds = DELEGATION_TTL_SECONDS
    unsigned = app_envelope.new_envelope(
        sender=sender_id,
        recipient=recipient_id,
        message_type="response",
        payload=payload,
        timestamp=_now_iso(timestamp) if timestamp is not None else None,
        expires_at=_now_iso(expires_at) if expires_at is not None else None,
        message_id=message_id,
        correlation_id=correlation_id,
        ttl_seconds=ttl_seconds,
    )
    signed = app_envelope.sign(unsigned, signer_priv)
    _store(conn, signed, "sent", _now_iso(timestamp))
    return signed

# --- inbound ---------------------------------------------------------------

def _task_delegation(payload: dict[str, Any]) -> dict[str, str] | None:
    """Detect a task-delegation request inside an ALLOWed ask.

    ``orchestration.dispatch_task`` always embeds ``capability_id``
    (plus ``delegation_id``/``target_agent``/``task_id``) in the signed
    payload. Plain asks carry no capability and keep the legacy
    ``allowed`` path. Returns the delegation fields, or None.
    """
    capability_id = str(payload.get("capability_id") or "").strip()
    if not capability_id:
        return None
    return {
        "capability_id": capability_id,
        "delegation_id": str(payload.get("delegation_id") or "").strip(),
        "target_agent": str(payload.get("target_agent") or "").strip(),
        "origin_task_id": str(payload.get("task_id") or "").strip(),
    }


def _run_blocking(coro):
    """Drive one coroutine from sync code (the receive path is sync).

    No running loop (HTTP routes, tests): ``asyncio.run`` inline. A
    loop is already running (the ``/ask/live`` pump): run on a private
    loop in a helper thread so the caller's loop never deadlocks. The
    sqlite connection is ``check_same_thread=False`` and the caller
    blocks here, so the connection is never touched concurrently.
    """
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _execute_remote_task(
    conn: sqlite3.Connection,
    live,
    payload: dict[str, Any],
    delegated: dict[str, str],
    *,
    signer_priv,
    local_id: str,
    at: str,
) -> dict[str, Any]:
    """Run an ALLOWed remote task-delegation through the local runner.

    Mirrors ``orchestration.dispatch_task``'s local branch in reverse:
    resolve the executing local agent, persist a local task row (so the
    run is observable here), execute the capability through the
    execution gate, then answer with a signed ``response`` — or a
    signed ``error`` the originator maps to ``REMOTE_<code>``. The
    inbound message row reaches a terminal status either way: the
    originator never times out on our side.

    The reply is signed by the executing agent (resolved via the
    machine secret, like the send path) so the originator's
    correlation checks accept it. Callers deliver ``reply`` with
    ``app.dispatch.queue_delivery`` — the same contract as the DENY
    path. Task shapes that cannot run here fail fast with a signed
    ``NOT_SUPPORTED`` instead of parking.
    """
    from app import agents
    from app import tasks as task_tracker
    from app.execution import ExecutionError, execute_capability

    capability_id = delegated["capability_id"]
    delegation_id = delegated["delegation_id"]
    purpose = str(payload.get("purpose") or "answer")
    data_category = str(payload.get("data_category", "general"))
    question = str(payload.get("question", ""))

    def _signed_error(code: str, message: str, *,
                      priv=None, sender: str | None = None):
        reply = app_envelope.signed_error(
            priv if priv is not None else signer_priv,
            sender=sender or local_id,
            recipient=live.sender,
            correlation_id=live.correlation_id,
            code=code,
            message=message,
        )
        _store(conn, reply, "sent", at)
        conn.execute(
            "UPDATE a2a_messages SET status = 'failed' "
            "WHERE message_id = ?",
            (live.message_id,),
        )
        conn.commit()
        return {"outcome": "error", "approval": None, "reply": reply}

    # The executor is a LOCAL agent: the addressed recipient first,
    # then the payload's target_agent (covers sub-agent addressing).
    # A paired peer can never execute here.
    acting = None
    for ref in (live.recipient, delegated["target_agent"]):
        if not ref:
            continue
        try:
            candidate = agents.resolve_agent(conn, ref)
        except agents.AgentError:
            continue
        if candidate["status"] == "active":
            acting = candidate
            break
    if acting is None:
        return _signed_error(
            "NOT_SUPPORTED",
            "no active local agent can execute this task "
            f"(recipient {live.recipient!r}).",
        )

    try:
        from app.machine_config import get_or_create_identity_secret

        exec_priv, exec_id = agents.resolve_signing_key(
            conn, get_or_create_identity_secret(), acting["id"]
        )
    except agents.AgentError as exc:
        return _signed_error(
            "NOT_SUPPORTED",
            f"executing agent {acting['id']!r} has no usable key: {exc}",
        )

    # Persist the remote run locally (observability + correlation).
    task = task_tracker.create_task(
        conn,
        owner_id="remote",
        requesting_agent_id=live.sender,
        target_agent=acting["agent_id"],
        capability=capability_id,
        purpose=purpose,
        task_input={
            "question": question,
            "data_category": data_category,
            "origin_task_id": delegated["origin_task_id"],
            "origin_correlation_id": live.correlation_id,
        },
        delegation_id=delegation_id,
        origin="remote",
        idempotency_key=f"a2a:{live.message_id}",
    )
    task_tracker.transition(
        conn, task["task_id"], "RESOLVING", detail="remote delegation"
    )
    task_tracker.transition(
        conn, task["task_id"], "AUTHORIZED", detail="a2a policy ALLOW"
    )
    task_tracker.transition(
        conn, task["task_id"], "DISPATCHED", detail=live.message_id
    )
    task_tracker.transition(conn, task["task_id"], "RUNNING", detail="local")

    # Fail fast on task shapes that cannot run here (mirrors the
    # execution gate's checks, with the explicit NOT_SUPPORTED code).
    from app import capabilities

    try:
        cap = capabilities.get_capability(conn, capability_id)
    except capabilities.CapabilityError:
        task_tracker.fail_task(
            conn, task["task_id"], "NOT_SUPPORTED",
            f"unknown capability {capability_id!r} on this host.",
        )
        return _signed_error(
            "NOT_SUPPORTED",
            f"capability {capability_id!r} is not registered here.",
            priv=exec_priv, sender=exec_id,
        )
    if cap["agent_id"] != acting["agent_id"]:
        task_tracker.fail_task(
            conn, task["task_id"], "NOT_SUPPORTED",
            f"capability {capability_id!r} is not owned by "
            f"{acting['id']!r}.",
        )
        return _signed_error(
            "NOT_SUPPORTED",
            f"capability {capability_id!r} is not owned by the "
            f"addressed agent {acting['id']!r}.",
            priv=exec_priv, sender=exec_id,
        )
    if cap["status"] != "active":
        task_tracker.fail_task(
            conn, task["task_id"], "NOT_SUPPORTED",
            f"capability {capability_id!r} is not active.",
        )
        return _signed_error(
            "NOT_SUPPORTED",
            f"capability {capability_id!r} is not active here.",
            priv=exec_priv, sender=exec_id,
        )
    if not cap.get("tool"):
        task_tracker.fail_task(
            conn, task["task_id"], "NOT_SUPPORTED",
            f"capability {capability_id!r} binds no executable tool.",
        )
        return _signed_error(
            "NOT_SUPPORTED",
            f"capability {capability_id!r} binds no executable tool "
            "(descriptive only).",
            priv=exec_priv, sender=exec_id,
        )

    args = {"question": question, "data_category": data_category}
    try:
        out = _run_blocking(
            execute_capability(
                conn,
                acting_ref=acting["id"],
                capability_id=capability_id,
                args=args,
                requester_ref=live.sender,
                purpose=purpose,
                delegation_id=delegation_id or None,
                task_id=live.correlation_id,
            )
        )
    except ExecutionError as exc:
        task_tracker.fail_task(conn, task["task_id"], exc.code, str(exc))
        return _signed_error(
            exc.code, str(exc)[:500], priv=exec_priv, sender=exec_id
        )
    done = task_tracker.complete_task(
        conn,
        task["task_id"],
        {
            "capability_id": capability_id,
            "agent_id": exec_id,
            "correlation_id": live.correlation_id,
            "result": out.get("result"),
        },
    )
    result = out.get("result")
    answer = (
        result
        if isinstance(result, str)
        else json.dumps(result, default=str)
    )
    reply = send_response(
        conn,
        signer_priv=exec_priv,
        sender_id=exec_id,
        recipient_id=live.sender,
        correlation_id=live.correlation_id,
        answer=answer,
        remote_task_id=done["task_id"],
    )
    conn.execute(
        "UPDATE a2a_messages SET status = 'executed' WHERE message_id = ?",
        (live.message_id,),
    )
    conn.commit()
    return {"outcome": "executed", "approval": None, "reply": reply}


def receive_envelope(
    conn: sqlite3.Connection,
    envelope: dict[str, Any],
    *,
    signer_priv,
    local_id: str,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Verify + enforce + route one incoming envelope.

    Returns ``{"outcome", "approval", "reply"}`` where outcome is one of
    parked / auto_allow / denied / resolved / answered / executed /
    error. ``executed`` means an ALLOWed task-delegation ran locally and
    ``reply`` carries the signed ``response`` (or signed ``error``);
    callers deliver ``reply`` via ``app.dispatch.queue_delivery``.
    Raises :class:`A2AError` for expired, unpaired, replayed, or
    badly-signed envelopes; unknown types return a signed error reply.
    """
    at = _now_iso(now)
    if not isinstance(envelope, dict):
        raise A2AError(
            "INVALID_ENVELOPE", "envelope must be an object.", status=400
        )
    correlation_id = envelope.get("correlation_id") or "corr_unknown"

    def _deny(code: str, message: str) -> dict[str, Any]:
        reply = app_envelope.signed_error(
            signer_priv,
            sender=local_id,
            recipient=envelope.get("sender", local_id),
            correlation_id=correlation_id
            if isinstance(correlation_id, str)
            else "corr_unknown",
            code=code,
            message=message,
        )
        return {"outcome": "error", "approval": None, "reply": reply}

    try:
        live = relay_envelope.Envelope.validate_live(envelope, now=at)
    except ValidationError as exc:
        raw_type = envelope.get("message_type")
        if isinstance(raw_type, str) and raw_type not in (
            relay_envelope.MESSAGE_TYPES
        ):
            return _deny(
                "UNKNOWN_TYPE",
                f"unknown message_type {raw_type!r}; "
                f"expected one of {list(relay_envelope.MESSAGE_TYPES)}.",
            )
        raise A2AError(
            "INVALID_ENVELOPE", f"envelope rejected: {exc}", status=400
        ) from exc
    except relay_envelope.EnvelopeError as exc:
        raise A2AError(exc.code, str(exc), status=400) from exc

    if not _is_local_recipient(conn, live.recipient, local_id):
        raise A2AError(
            "INVALID_ENVELOPE",
            "envelope is not addressed to this agent.",
            status=400,
        )
    sender_key = _sender_public_key(conn, live.sender)
    if not app_envelope.verify(dict(envelope), sender_key):
        # Verify BEFORE storing: a bad signature must not consume the
        # message_id replay slot, or a poison envelope burns the PK and
        # a later legitimate reuse gets REPLAY 409.
        raise A2AError(
            "INVALID_SIGNATURE",
            "envelope signature does not verify against the known key.",
            status=401,
        )
    _store(conn, dict(envelope), "stored", at)
    from app import pairing as pairing_mod

    pairing_mod.touch_peer_seen(conn, live.sender, at)

    payload = dict(live.payload)
    if live.message_type == "request":
        controlled = _handle_control_action(conn, live, payload, at)
        if controlled is not None:
            return controlled
    if live.message_type == "request":
        decision = evaluate_policy(
            conn,
            peer=live.sender,
            data_category=str(payload.get("data_category", "general")),
            purpose=str(payload.get("purpose", "answer")),
            action=str(payload.get("action", "answer")),
        )
        if decision == "ALLOW":
            delegated = _task_delegation(payload)
            if delegated is not None:
                # Task delegation: execute through the local runner and
                # answer — never the 'allowed' dead-end.
                return _execute_remote_task(
                    conn, live, payload, delegated,
                    signer_priv=signer_priv, local_id=local_id, at=at,
                )
            conn.execute(
                "UPDATE a2a_messages SET status = 'allowed' "
                "WHERE message_id = ?",
                (live.message_id,),
            )
            conn.commit()
            return {"outcome": "auto_allow", "approval": None, "reply": None}
        if decision == "DENY":
            reply = app_envelope.signed_error(
                signer_priv,
                sender=local_id,
                recipient=live.sender,
                correlation_id=live.correlation_id,
                code="DENIED",
                message=(
                    f"category {payload.get('data_category')!r} is "
                    "sensitive and never auto-disclosed."
                ),
            )
            _store(conn, reply, "sent", at)
            return {"outcome": "denied", "approval": None, "reply": reply}
        card = _park_approval(
            conn,
            message_id=live.message_id,
            correlation_id=live.correlation_id,
            requester=live.sender,
            payload=payload,
            expires_at=live.expires_at,
            now=at,
        )
        return {"outcome": "parked", "approval": card, "reply": None}

    if live.message_type == "approval_request":
        card = _park_approval(
            conn,
            message_id=live.message_id,
            correlation_id=live.correlation_id,
            requester=live.sender,
            payload=payload,
            expires_at=live.expires_at,
            now=at,
        )
        return {"outcome": "parked", "approval": card, "reply": None}

    if live.message_type in RESOLUTION_TYPES:
        # An approval decision must answer OUR request to THIS peer: a
        # third party naming our correlation ID cannot resolve our cards.
        origin = conn.execute(
            "SELECT message_id FROM a2a_messages WHERE correlation_id = ?"
            " AND sender = ? AND recipient = ?",
            (live.correlation_id, local_id, live.sender),
        ).fetchone()
        if origin is None:
            raise A2AError(
                "UNCORRELATED",
                "decision matches no request we sent to this peer.",
                status=400,
            )
        decided = (
            "approved" if live.message_type == "approve" else "rejected"
        )
        with conn:
            conn.execute(
                "UPDATE a2a_approvals SET status = ?, decided_at = ? "
                "WHERE correlation_id = ? AND status = 'pending'",
                (decided, at, live.correlation_id),
            )
        return {"outcome": "resolved", "approval": None, "reply": None}

    if live.message_type == "response":
        # An answer must continue a conversation WE started with THIS
        # peer: require our own outbound row with the same correlation.
        # Unsolicited "answers" (wrong peer, invented correlation) fail
        # closed instead of landing silently in history.
        prior = conn.execute(
            "SELECT message_id FROM a2a_messages WHERE correlation_id = ?"
            " AND sender = ? AND recipient = ?",
            (live.correlation_id, local_id, live.sender),
        ).fetchone()
        if prior is None:
            raise A2AError(
                "UNCORRELATED",
                "response matches no request we sent to this peer.",
                status=400,
            )
        # Orchestrated tasks awaiting this correlation complete with the
        # structured answer (never NLP-parsed); late answers for
        # terminal tasks are ignored inside, and done parents close.
        from app import tasks as task_tracker

        remote_task = payload.get("remote_task_id") or ""
        completed = task_tracker.complete_by_correlation(
            conn,
            live.correlation_id,
            {
                "answer": payload.get("answer"),
                "sender": live.sender,
                "correlation_id": live.correlation_id,
                "remote_task_id": remote_task
                if isinstance(remote_task, str) else "",
            },
        )
        if completed is not None and completed.get("parent_task_id"):
            task_tracker.close_parent_if_done(
                conn, completed["parent_task_id"])
        return {"outcome": "answered", "approval": None, "reply": None}

    if live.message_type == "error":
        # Remote-side failure for one of our tasks: record it with its
        # origin preserved (REMOTE_<code>), but only when the sender is
        # the task's expected target — never a stranger's task.
        from app import tasks as task_tracker

        try:
            match = task_tracker.get_task_by_correlation(
                conn, live.correlation_id)
        except task_tracker.TaskError:
            match = None
        if (match is not None
                and match["status"] not in task_tracker.TERMINAL
                and match["target_agent_id"] == live.sender):
            code = str(payload.get("code", "UNKNOWN") or "UNKNOWN")
            task_tracker.fail_task(
                conn, match["task_id"], f"REMOTE_{code}",
                f"remote {live.sender}: "
                f"{payload.get('message', '')}"[:1500])
            task_tracker.close_parent_if_done(
                conn, match["parent_task_id"])
        return {"outcome": "error", "approval": None, "reply": None}

    return _deny(
        "UNKNOWN_TYPE", f"unknown message_type {live.message_type!r}."
    )


# --- approvals ----------------------------------------------------------------

def _get_approval(conn: sqlite3.Connection, approval_id: str) -> dict:
    row = conn.execute(
        "SELECT approval_id, message_id, correlation_id, requester, "
        "action, data_category, purpose, question, expires_at, status, "
        "decided_at, created_at FROM a2a_approvals WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    if row is None:
        raise A2AError(
            "NOT_FOUND", "approval not found.", status=404
        )
    return dict(row)


def _check_approval_expiry(card: dict[str, Any], at: str) -> None:
    """Reject expired or unparseable parked approvals (normalized)."""
    raw = card.get("expires_at")
    if raw is None or (isinstance(raw, str) and raw == ""):
        raise A2AError(
            "INVALID",
            "approval has no expiry.",
            status=400,
        )
    try:
        exp = relay_envelope.parse_iso(raw)
        now_dt = relay_envelope.parse_iso(at)
    except (relay_envelope.EnvelopeError, TypeError, ValueError) as exc:
        raise A2AError(
            "INVALID",
            f"approval expiry is invalid: {exc}",
            status=410,
        ) from exc
    if exp <= now_dt:
        raise A2AError(
            "EXPIRED",
            "approval has expired.",
            status=410,
        )


def park_local_approval(
    conn: sqlite3.Connection,
    *,
    requester: str,
    action: str,
    question: str,
    correlation_id: str,
    purpose: str = "answer",
    data_category: str = "general",
    expires_in_seconds: int = 900,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Park an owner-decision card for orchestrator-driven work.

    Same row shape as inbound approvals, so the existing decide API
    (`POST /ask/approvals/{id}/approve`) and expiry rules apply
    unchanged. Binding to task/agent/capability lives on the task row
    (`approval_id` + shared `correlation_id`), not the card.
    """
    at = _now_iso(now)
    expires = _iso(
        relay_envelope.parse_iso(at)
        + timedelta(seconds=max(60, int(expires_in_seconds or 900)))
    )
    return _park_approval(
        conn,
        message_id=f"msg_task_{uuid.uuid4().hex[:12]}",
        correlation_id=correlation_id,
        requester=requester,
        payload={
            "action": action,
            "question": question,
            "purpose": purpose,
            "data_category": data_category,
        },
        expires_at=expires,
        now=at,
    )


def list_approvals(
    conn: sqlite3.Connection, status: str = "pending"
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT approval_id, message_id, correlation_id, requester, "
        "action, data_category, purpose, question, expires_at, status, "
        "decided_at, created_at FROM a2a_approvals WHERE status = ? "
        "ORDER BY created_at ASC",
        (status,),
    ).fetchall()
    return [dict(row) for row in rows]


def approve_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    signer_priv,
    local_id: str,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Resolve a parked card as approved; return the signed approve."""
    at = _now_iso(now)
    card = _get_approval(conn, approval_id)
    if card["status"] != "pending":
        raise A2AError(
            "ALREADY_DECIDED",
            f"approval is already {card['status']}.",
            status=409,
        )
    _check_approval_expiry(card, at)
    with conn:
        conn.execute(
            "UPDATE a2a_approvals SET status = 'approved', "
            "decided_at = ? WHERE approval_id = ?",
            (at, approval_id),
        )
    unsigned = app_envelope.new_envelope(
        sender=local_id,
        recipient=card["requester"],
        message_type="approve",
        payload={"approved": True, "scope": "answer-once"},
        correlation_id=card["correlation_id"],
        timestamp=at,
        expires_at=_iso(
            relay_envelope.parse_iso(at) + timedelta(seconds=300)
        ),
    )
    signed = app_envelope.sign(unsigned, signer_priv)
    _store(conn, signed, "sent", at)
    return signed


def reject_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    signer_priv,
    local_id: str,
    reason: str = "declined by approver",
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Resolve a parked card as rejected; return the signed reject."""
    at = _now_iso(now)
    card = _get_approval(conn, approval_id)
    if card["status"] != "pending":
        raise A2AError(
            "ALREADY_DECIDED",
            f"approval is already {card['status']}.",
            status=409,
        )
    _check_approval_expiry(card, at)
    with conn:
        conn.execute(
            "UPDATE a2a_approvals SET status = 'rejected', "
            "decided_at = ? WHERE approval_id = ?",
            (at, approval_id),
        )
    unsigned = app_envelope.new_envelope(
        sender=local_id,
        recipient=card["requester"],
        message_type="reject",
        payload={"approved": False, "reason": reason},
        correlation_id=card["correlation_id"],
        timestamp=at,
        expires_at=_iso(
            relay_envelope.parse_iso(at) + timedelta(seconds=300)
        ),
    )
    signed = app_envelope.sign(unsigned, signer_priv)
    _store(conn, signed, "sent", at)
    return signed


def list_messages(
    conn: sqlite3.Connection, correlation_id: str | None = None
) -> list[dict[str, Any]]:
    """Stored envelopes (sent + received), oldest first."""
    if correlation_id is not None:
        rows = conn.execute(
            "SELECT message_id, correlation_id, sender, recipient, "
            "message_type, envelope_json, status, created_at "
            "FROM a2a_messages WHERE correlation_id = ? "
            "ORDER BY created_at ASC",
            (correlation_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT message_id, correlation_id, sender, recipient, "
            "message_type, envelope_json, status, created_at "
            "FROM a2a_messages ORDER BY created_at ASC LIMIT 200"
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["envelope"] = json.loads(item.pop("envelope_json"))
        out.append(item)
    return out


__all__ = [
    "RESOLUTION_TYPES",
    "REQUEST_TYPES",
    "SENSITIVE_CATEGORIES",
    "A2AError",
    "approve_approval",
    "create_request",
    "create_rule",
    "delete_rule",
    "evaluate_policy",
    "list_approvals",
    "list_messages",
    "list_rules",
    "receive_envelope",
    "reject_approval",
    "send_response",
]
