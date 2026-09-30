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
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.a2a import envelope as app_envelope
from relay import envelope as relay_envelope

REQUEST_TYPES = ("request", "approval_request")
RESOLUTION_TYPES = ("approve", "reject")

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
) -> str:
    """ALLOW when a rule matches (most specific wins), DENY when the
    category is sensitive, ASK otherwise (fail-closed default)."""
    if data_category in SENSITIVE_CATEGORIES:
        return "DENY"
    best: int | None = None
    for row in conn.execute(
        "SELECT peer, data_category, purpose, action, effect "
        "FROM policy_rules WHERE effect = 'ALLOW'"
    ).fetchall():
        fields = (
            (row["peer"], peer),
            (row["data_category"], data_category),
            (row["purpose"], purpose),
            (row["action"], action),
        )
        if any(rule != "*" and rule != got for rule, got in fields):
            continue
        score = sum(1 for rule, _ in fields if rule != "*")
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
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Store a user-created ALLOW rule (the only effect in v1)."""
    if effect != "ALLOW":
        raise A2AError(
            "INVALID_RULE",
            "v1 policy rules are ALLOW-only (DENY is reserved for "
            "sensitive categories).",
            status=400,
        )
    rule_id = _new_id("rule")
    created_at = _now_iso(now)
    with conn:
        conn.execute(
            "INSERT INTO policy_rules "
            "(rule_id, peer, data_category, purpose, action, effect, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rule_id, peer, data_category, purpose, action, effect,
             created_at),
        )
    return {
        "rule_id": rule_id,
        "peer": peer,
        "data_category": data_category,
        "purpose": purpose,
        "action": action,
        "effect": effect,
        "created_at": created_at,
    }


def list_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT rule_id, peer, data_category, purpose, action, effect, "
        "created_at FROM policy_rules ORDER BY created_at ASC"
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
        "SELECT agent_id, public_key FROM paired_peers WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        raise A2AError(
            "NOT_PAIRED",
            "unknown peer: pair before exchanging messages.",
            status=404,
        )
    return row


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
) -> dict[str, Any]:
    """Sign an ask to a paired peer and store it as sent."""
    if message_type not in REQUEST_TYPES:
        raise A2AError(
            "INVALID_TYPE",
            f"ask types are {list(REQUEST_TYPES)}.",
            status=400,
        )
    _pinned_peer(conn, recipient_id)
    unsigned = app_envelope.new_envelope(
        sender=sender_id,
        recipient=recipient_id,
        message_type=message_type,
        payload={
            "action": action,
            "question": question,
            "data_category": data_category,
            "purpose": purpose,
        },
        timestamp=_now_iso(timestamp) if timestamp is not None else None,
        expires_at=_now_iso(expires_at) if expires_at is not None else None,
        message_id=message_id,
        correlation_id=correlation_id,
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
) -> dict[str, Any]:
    """Sign an answer to a paired peer (post-approval) and store it."""
    _pinned_peer(conn, recipient_id)
    unsigned = app_envelope.new_envelope(
        sender=sender_id,
        recipient=recipient_id,
        message_type="response",
        payload={"action": "answer", "answer": answer},
        timestamp=_now_iso(timestamp) if timestamp is not None else None,
        expires_at=_now_iso(expires_at) if expires_at is not None else None,
        message_id=message_id,
        correlation_id=correlation_id,
    )
    signed = app_envelope.sign(unsigned, signer_priv)
    _store(conn, signed, "sent", _now_iso(timestamp))
    return signed

# --- inbound ---------------------------------------------------------------

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
    parked / auto_allow / denied / resolved / answered / error.
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

    if live.recipient != local_id:
        raise A2AError(
            "INVALID_ENVELOPE",
            "envelope is not addressed to this agent.",
            status=400,
        )
    peer = _pinned_peer(conn, live.sender)
    _store(conn, dict(envelope), "stored", at)
    if not app_envelope.verify(dict(envelope), peer["public_key"]):
        raise A2AError(
            "INVALID_SIGNATURE",
            "envelope signature does not verify against the pinned key.",
            status=401,
        )

    payload = dict(live.payload)
    if live.message_type == "request":
        decision = evaluate_policy(
            conn,
            peer=live.sender,
            data_category=str(payload.get("data_category", "general")),
            purpose=str(payload.get("purpose", "answer")),
            action=str(payload.get("action", "answer")),
        )
        if decision == "ALLOW":
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
        return {"outcome": "answered", "approval": None, "reply": None}

    if live.message_type == "error":
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
