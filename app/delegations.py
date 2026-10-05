"""Delegation grants: explicit, signed, expiring, revocable authorization.

A grant says: issuer Agent A authorizes recipient Agent B to perform
capability X for purpose Y (optionally bound to one task), until time
Z, under constraints. Grants are SIGNED by the issuer (canonical JSON,
same construction as agent cards) so any party holding the issuer's
public key — local registry or pinned peer card — can verify them
without trusting the presenter.

Grants never bypass the recipient's own policy engine: execution
evaluates local policy with the issuer as the peer, and a grant with
``{"require_approval": true}`` additionally demands a decided-approve
approval row for the bound task. Delegation is authorization, never
permanent trust: expiry and revocation are enforced on every use.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

GRANT_TYPE = "delegation-grant"
GRANT_PROTOCOL = "nexus-a2a"
GRANT_VERSION = "0.3"


class DelegationError(RuntimeError):
    """Delegation failure with machine ``code`` + HTTP ``status``."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(raw: str, field: str) -> Any:
    try:
        return json.loads(raw or "{}")
    except ValueError as exc:
        raise DelegationError(
            "CORRUPT", f"stored {field} is not JSON: {exc}", status=500
        ) from exc


#: Exact fields covered by the issuer signature. Fixed (not "all
#: minus signature") so database round-trips — which add created_at,
#: status, revoked_at — verify byte-identically to the issued grant.
SIGNED_GRANT_FIELDS = frozenset({
    "type", "protocol", "version", "id", "issuer_agent_id",
    "recipient_agent_id", "capability_id", "purpose", "task_id",
    "constraints", "issued_at", "expires_at",
})


def _canonical_grant(grant: dict[str, Any]) -> bytes:
    from relay.envelope import canonical_json_bytes

    # Protocol constants are filled, never trusted from the dict: stored
    # grants don't persist them, and a presenter must not be able to
    # alter the signed domain.
    unsigned = {
        "type": GRANT_TYPE,
        "protocol": GRANT_PROTOCOL,
        "version": GRANT_VERSION,
        **{k: v for k, v in grant.items() if k in SIGNED_GRANT_FIELDS},
    }
    return canonical_json_bytes(unsigned)


def _issuer_public_key(
    conn: sqlite3.Connection, issuer_agent_id: str
) -> bytes:
    """Issuer's current public key: local registry first, paired peers."""
    row = conn.execute(
        "SELECT public_key FROM agent_identities"
        " WHERE agent_id = ? AND status = 'active'"
        " ORDER BY version DESC LIMIT 1",
        (issuer_agent_id,),
    ).fetchone()
    if row is not None:
        return base64.b64decode(row["public_key"].encode("ascii"))
    peer = conn.execute(
        "SELECT public_key FROM paired_peers WHERE agent_id = ?",
        (issuer_agent_id,),
    ).fetchone()
    if peer is not None:
        return base64.b64decode(peer["public_key"].encode("ascii"))
    raise DelegationError(
        "UNKNOWN_ISSUER",
        f"no local or pinned key for issuer {issuer_agent_id!r}.",
        status=400,
    )


def verify_grant_signature(conn: sqlite3.Connection, grant: dict) -> None:
    """Verify a grant's issuer signature; raises on any fault."""
    from app.identity import crypto

    signature_b64 = grant.get("signature", "")
    if not isinstance(signature_b64, str) or not signature_b64:
        raise DelegationError(
            "BAD_SIGNATURE", "grant has no signature.", status=400
        )
    try:
        public_key = crypto.load_public_key(
            _issuer_public_key(conn, grant.get("issuer_agent_id", ""))
        )
        signature = base64.b64decode(signature_b64.encode("ascii"))
    except DelegationError:
        raise
    except Exception as exc:
        raise DelegationError(
            "BAD_SIGNATURE", f"grant key material invalid: {exc}",
            status=400,
        ) from exc
    if not crypto.verify_bytes(public_key, _canonical_grant(grant), signature):
        raise DelegationError(
            "BAD_SIGNATURE",
            "grant signature does not verify against the issuer key.",
            status=400,
        )


def _row_to_grant(row: sqlite3.Row) -> dict[str, Any]:
    grant = dict(row)
    grant["constraints"] = _parse_json(
        grant.pop("constraints_json", "{}"), "constraints"
    )
    return grant


def get_grant(conn: sqlite3.Connection, grant_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, issuer_agent_id, recipient_agent_id, capability_id,"
        " purpose, task_id, constraints_json, issued_at, expires_at,"
        " revoked_at, status, signature, created_at FROM delegations"
        " WHERE id = ?",
        (grant_id,),
    ).fetchone()
    if row is None:
        raise DelegationError(
            "NOT_FOUND", f"unknown delegation {grant_id!r}.", status=404
        )
    return _row_to_grant(row)


def _record(conn: sqlite3.Connection, grant_id: str, action: str,
            detail: str = "") -> None:
    conn.execute(
        "INSERT INTO delegation_events (event_id, delegation_id, action,"
        " detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (f"dev_{uuid.uuid4().hex[:12]}", grant_id, action, detail, _now()),
    )


def _uses(conn: sqlite3.Connection, grant_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM delegation_events"
        " WHERE delegation_id = ? AND action = 'used'",
        (grant_id,),
    ).fetchone()
    return int(row[0])


def _known_agent_or_peer(conn: sqlite3.Connection, agent_id: str) -> bool:
    if conn.execute(
        "SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone():
        return True
    return bool(
        conn.execute(
            "SELECT 1 FROM paired_peers WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    )


def _agent_usable(conn: sqlite3.Connection, agent_id: str) -> bool:
    """Local agents must be active; paired peers must be TRUSTED.

    Suspending or revoking a peer immediately invalidates their grants
    here — revocation propagates through the trust state, bounded above
    by grant expiry. Unknown revocation state never counts as valid.
    """
    row = conn.execute(
        "SELECT status FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is not None:
        return row["status"] == "active"
    peer = conn.execute(
        "SELECT trust_state FROM paired_peers WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if peer is None:
        return False
    state = peer["trust_state"] if "trust_state" in peer.keys() else "TRUSTED"
    return state == "TRUSTED"


def issue_grant(
    conn: sqlite3.Connection,
    secret: str,
    issuer_ref: str,
    recipient_agent_id: str,
    capability_id: str,
    purpose: str,
    *,
    task_id: str = "",
    constraints: dict | None = None,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """Issuer (local, active, keyed) authorizes recipient for capability.

    Recipient must be a known local agent or paired peer; capability must
    be registered to the recipient when the recipient is local (remote
    capabilities are advertised on their card, not our registry).
    """
    from app import agents, capabilities
    from app.identity import crypto

    if not (purpose or "").strip():
        raise DelegationError(
            "BAD_PURPOSE", "purpose must not be empty.", status=400
        )
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise DelegationError(
            "BAD_TTL", "ttl_seconds must be a positive integer.", status=400
        )
    try:
        constraints_json = json.dumps(constraints or {})
    except (TypeError, ValueError) as exc:
        raise DelegationError(
            "BAD_CONSTRAINTS", f"constraints not JSON-serializable: {exc}",
            status=400,
        ) from exc
    issuer = agents.resolve_agent(conn, issuer_ref)
    if issuer["status"] != "active":
        raise DelegationError(
            "ISSUER_INACTIVE",
            f"issuer {issuer['id']!r} is {issuer['status']}.",
            status=403,
        )
    if not _known_agent_or_peer(conn, recipient_agent_id):
        raise DelegationError(
            "UNKNOWN_RECIPIENT",
            "recipient is neither a local agent nor a paired peer.",
            status=404,
        )
    issuer_id = issuer["agent_id"]
    if recipient_agent_id == issuer_id:
        raise DelegationError(
            "SELF_GRANT", "issuer and recipient must differ.", status=400
        )
    from app import discovery

    is_local = bool(
        conn.execute(
            "SELECT 1 FROM agents WHERE agent_id = ?",
            (recipient_agent_id,),
        ).fetchone()
    )
    if is_local:
        # Recipient on this host: capability must be theirs and active.
        try:
            local_cap = capabilities.get_capability(conn, capability_id)
        except capabilities.CapabilityError as exc:
            raise DelegationError(
                exc.code, str(exc), status=exc.status) from exc
        if local_cap["agent_id"] != recipient_agent_id:
            raise DelegationError(
                "WRONG_CAPABILITY_OWNER",
                "capability is not registered to the recipient.",
                status=400,
            )
        if local_cap["status"] != "active":
            raise DelegationError(
                "CAPABILITY_DISABLED", "capability is not active.",
                status=409,
            )
    elif capability_id not in discovery.advertised_capabilities(
        conn, recipient_agent_id
    ):
        # Remote recipient: the capability must be advertised on their
        # pinned card (verified at pairing). Unknown capabilities fail.
        raise DelegationError(
            "UNKNOWN_CAPABILITY",
            "recipient neither registers nor advertises this capability.",
            status=400,
        )
    private_key, _ = agents.resolve_signing_key(conn, secret, issuer_id)
    now = datetime.now(timezone.utc)
    grant_id = f"dlg_{uuid.uuid4().hex[:12]}"
    grant = {
        "type": GRANT_TYPE,
        "protocol": GRANT_PROTOCOL,
        "version": GRANT_VERSION,
        "id": grant_id,
        "issuer_agent_id": issuer_id,
        "recipient_agent_id": recipient_agent_id,
        "capability_id": capability_id,
        "purpose": purpose.strip(),
        "task_id": task_id.strip(),
        "constraints": constraints or {},
        "issued_at": now.isoformat(),
        "expires_at": (
            now + timedelta(seconds=ttl_seconds)
        ).isoformat(),
    }
    raw_sig = crypto.sign_bytes(private_key, _canonical_grant(grant))
    grant["signature"] = base64.b64encode(raw_sig).decode("ascii")
    with conn:
        conn.execute(
            "INSERT INTO delegations (id, issuer_agent_id,"
            " recipient_agent_id, capability_id, purpose, task_id,"
            " constraints_json, issued_at, expires_at, revoked_at,"
            " status, signature, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'active', ?, ?)",
            (
                grant_id, issuer_id, recipient_agent_id, capability_id,
                grant["purpose"], grant["task_id"], constraints_json,
                grant["issued_at"], grant["expires_at"], grant["signature"],
                grant["issued_at"],
            ),
        )
        _record(conn, grant_id, "issued",
                f"{issuer['id']} -> {recipient_agent_id} {capability_id}")
    stored = get_grant(conn, grant_id)
    stored["signature"] = grant["signature"]
    return stored


def receive_grant(
    conn: sqlite3.Connection, grant: dict[str, Any]
) -> dict[str, Any]:
    """Record a remotely-issued grant after verifying it.

    Signature and expiry are verified against the issuer's pinned or
    registered key; unknown issuers and bad signatures are rejected, not
    stored. Re-presenting a known grant is idempotent. Stored grants are
    what execution and revocation act on, so use-counts and revocations
    have one home even for foreign issuers.
    """
    if not isinstance(grant, dict) or not grant.get("id"):
        raise DelegationError(
            "BAD_GRANT", "grant must be an object with an id.", status=400
        )
    verify_grant_signature(conn, grant)
    try:
        expires = datetime.fromisoformat(grant["expires_at"])
    except (ValueError, TypeError, KeyError) as exc:
        raise DelegationError(
            "BAD_GRANT", f"grant has invalid expiry: {exc}", status=400
        ) from exc
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise DelegationError(
            "EXPIRED_GRANT", "grant already expired.", status=400
        )
    existing = conn.execute(
        "SELECT id FROM delegations WHERE id = ?", (grant["id"],)
    ).fetchone()
    if existing is not None:
        return get_grant(conn, grant["id"])
    constraints = grant.get("constraints") or {}
    if not isinstance(constraints, dict):
        raise DelegationError(
            "BAD_GRANT", "grant constraints must be an object.", status=400
        )
    with conn:
        conn.execute(
            "INSERT INTO delegations (id, issuer_agent_id,"
            " recipient_agent_id, capability_id, purpose, task_id,"
            " constraints_json, issued_at, expires_at, revoked_at,"
            " status, signature, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'active', ?, ?)",
            (
                grant["id"], grant.get("issuer_agent_id", ""),
                grant.get("recipient_agent_id", ""),
                grant.get("capability_id", ""),
                grant.get("purpose", ""), grant.get("task_id", ""),
                json.dumps(constraints),
                grant.get("issued_at", _now()), grant["expires_at"],
                grant.get("signature", ""), _now(),
            ),
        )
        _record(conn, grant["id"], "received")
    return get_grant(conn, grant["id"])


def revoke_grant(conn: sqlite3.Connection, grant_id: str) -> dict[str, Any]:
    grant = get_grant(conn, grant_id)
    if grant["status"] == "revoked":
        return {"id": grant_id, "revoked": True}
    now = _now()
    with conn:
        conn.execute(
            "UPDATE delegations SET status = 'revoked', revoked_at = ?"
            " WHERE id = ?",
            (now, grant_id),
        )
        _record(conn, grant_id, "revoked")
    return {"id": grant_id, "revoked": True}


def evaluate_grant(
    conn: sqlite3.Connection,
    grant: dict[str, Any],
    *,
    recipient_agent_id: str,
    capability_id: str,
    purpose: str,
    task_id: str = "",
) -> tuple[bool, str]:
    """Check a grant covers this exact use. Returns (ok, reason)."""
    if grant.get("status") == "revoked" or grant.get("revoked_at"):
        return False, "delegation revoked"
    try:
        expires = datetime.fromisoformat(grant["expires_at"])
    except (ValueError, TypeError, KeyError):
        return False, "delegation has invalid expiry"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        return False, "delegation expired"
    if grant.get("recipient_agent_id") != recipient_agent_id:
        return False, "delegation names a different recipient"
    if grant.get("capability_id") != capability_id:
        return False, "delegation names a different capability"
    if (grant.get("purpose") or "").strip() != (purpose or "").strip():
        return False, "delegation names a different purpose"
    bound_task = (grant.get("task_id") or "").strip()
    if bound_task and bound_task != (task_id or "").strip():
        return False, "delegation is bound to a different task"
    constraints = grant.get("constraints") or {}
    if not isinstance(constraints, dict):
        return False, "delegation constraints corrupt"
    max_uses = constraints.get("max_uses")
    if isinstance(max_uses, int) and _uses(conn, grant["id"]) >= max_uses:
        return False, "delegation use budget exhausted"
    if not _agent_usable(conn, grant["issuer_agent_id"]):
        return False, "issuer agent no longer usable"
    if not _agent_usable(conn, recipient_agent_id):
        return False, "recipient agent no longer usable"
    return True, "ok"


def check_approval_binding(
    conn: sqlite3.Connection, grant: dict[str, Any], task_id: str
) -> tuple[bool, str]:
    """When the grant demands owner approval, require a decided-approve
    approval row for the bound task. No row (or wrong state) blocks."""
    constraints = grant.get("constraints") or {}
    if not constraints.get("require_approval"):
        return True, "no approval required"
    bound = (task_id or "").strip()
    if not bound:
        return False, "approval required but no task bound"
    row = conn.execute(
        "SELECT status FROM a2a_approvals WHERE correlation_id = ?"
        " ORDER BY rowid DESC LIMIT 1",
        (bound,),
    ).fetchone()
    if row is None or row["status"] != "approved":
        return False, "owner approval required for this task"
    return True, "ok"


__all__ = [
    "DelegationError",
    "check_approval_binding",
    "evaluate_grant",
    "get_grant",
    "issue_grant",
    "receive_grant",
    "revoke_grant",
    "verify_grant_signature",
]
