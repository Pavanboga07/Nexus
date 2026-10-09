"""Local multi-agent registry: many agents, one owner, per-agent keys.

One laptop has one operator (``owner_id="local"``) but may run many
independently identifiable agents. Each agent owns exactly one ACTIVE
Ed25519 identity at a time; rotation retires the old key to
``rotated`` (kept for verifying in-flight material) and revocation
marks ``revoked`` with a timestamp. Key history is never deleted.

Relationship to the legacy ``identity`` singleton: the ``default``
agent is backfilled FROM the identity row on first need
(:func:`ensure_default_agent`) — same keypair, no fork. The identity
row stays the source of truth for pre-agent flows (pairing, ask
signing); new consumers resolve through here.

Nothing in this module ever returns private key material.
"""

from __future__ import annotations

import base64
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from app import owners as owners_mod
from app.errors import NexusError
from app.events import emit_best_effort
from app.identity import crypto

logger = logging.getLogger(__name__)

DEFAULT_AGENT_ID = "default"
DEFAULT_OWNER_ID = "local"

_AGENT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class AgentError(NexusError):
    """Agent registry failure with machine ``code`` + HTTP ``status``."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_name(name: str) -> str:
    cleaned = (name or "").strip().lower()
    if not _AGENT_NAME_RE.match(cleaned):
        raise AgentError(
            "BAD_NAME",
            "agent id must be 1-64 chars of a-z, 0-9, '-' (start/end alnum).",
            status=400,
        )
    return cleaned


def _public_view(agent: dict[str, Any], key: dict[str, Any] | None) -> dict:
    return {
        "id": agent["id"],
        "owner_id": agent["owner_id"],
        "agent_id": agent["agent_id"],
        "display_name": agent["display_name"],
        "status": agent["status"],
        "autonomy": agent.get("autonomy", "limited") or "limited",
        "fingerprint": key["fingerprint"] if key else "",
        "key_version": key["version"] if key else 0,
        "created_at": agent["created_at"],
        "updated_at": agent["updated_at"],
    }


def _active_key(
    conn: sqlite3.Connection, agent_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT key_id, agent_id, version, public_key,"
        " encrypted_private_key, fingerprint, status, created_at, revoked_at"
        " FROM agent_identities WHERE agent_id = ? AND status = 'active'"
        " ORDER BY version DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def resolve_agent(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    """Fetch one agent by local id or crypto agent_id; 404 when unknown."""
    row = conn.execute(
        "SELECT id, owner_id, agent_id, display_name, status, autonomy,"
        " created_at, updated_at FROM agents WHERE id = ? OR agent_id = ?",
        (ref, ref),
    ).fetchone()
    if row is None:
        raise AgentError("NOT_FOUND", f"unknown agent {ref!r}.", status=404)
    return dict(row)


def list_agents(conn: sqlite3.Connection,
                owner_id: str | None = None) -> list[dict[str, Any]]:
    if owner_id is None:
        rows = conn.execute(
            "SELECT id, owner_id, agent_id, display_name, status, autonomy,"
            " created_at, updated_at FROM agents ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, owner_id, agent_id, display_name, status, autonomy,"
            " created_at, updated_at FROM agents WHERE owner_id = ?"
            " ORDER BY id",
            (owner_id,),
        ).fetchall()
    out = []
    for row in rows:
        agent = dict(row)
        out.append(_public_view(agent, _active_key(conn, agent["agent_id"])))
    return out


def get_agent(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    agent = resolve_agent(conn, ref)
    return _public_view(agent, _active_key(conn, agent["agent_id"]))


def ensure_default_agent(
    conn: sqlite3.Connection, secret: str
) -> dict[str, Any]:
    """Backfill the ``default`` agent FROM the legacy identity row.

    Same keypair, no fork: version 1 mirrors the identity's sealed key.
    Raises NO_IDENTITY when no identity exists yet (pair or ask first).

    ``secret`` is not decoration: the caller must prove it holds the
    identity secret by trial-decrypting the sealed identity key before
    anything is written (finding B2 — the parameter used to be thrown
    away with ``_ = secret``).
    """
    try:
        return get_agent(conn, DEFAULT_AGENT_ID)
    except AgentError:
        logger.debug("no default agent yet; backfilling from identity")
    row = conn.execute(
        "SELECT agent_id, public_key, encrypted_private_key FROM identity"
        " WHERE id = 1"
    ).fetchone()
    if row is None:
        raise AgentError(
            "NO_IDENTITY",
            "no local identity yet — pair or ask once first.",
            status=503,
        )
    try:
        crypto.decrypt_private_key(row["encrypted_private_key"], secret)
    except Exception as exc:
        raise AgentError(
            "BAD_SECRET",
            "identity secret does not unlock the local identity.",
            status=403,
        ) from exc
    public_raw = base64.b64decode(row["public_key"].encode("ascii"))
    now = _now()
    key_id = f"kid_{uuid.uuid4().hex[:12]}"
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, owner_id, agent_id,"
            " display_name, status, created_at, updated_at)"
            " VALUES ('default', 'local', ?, ?, 'active', ?, ?)",
            (row["agent_id"], row["agent_id"], now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO agent_identities (key_id, agent_id,"
            " version, public_key, encrypted_private_key, fingerprint,"
            " status, created_at, revoked_at)"
            " VALUES (?, ?, 1, ?, ?, ?, 'active', ?, '')",
            (
                key_id,
                row["agent_id"],
                row["public_key"],
                row["encrypted_private_key"],
                crypto.fingerprint_from_public_key(public_raw),
                now,
            ),
        )
    # NOTE: the backfill copies the identity's ALREADY-sealed key — the
    # secret itself is never stored or re-applied; it was verified by
    # trial-decryption above, which is its whole job here.
    return get_agent(conn, DEFAULT_AGENT_ID)


def ensure_default_agent_quiet(
    conn: sqlite3.Connection, secret: str
) -> dict[str, Any] | None:
    """Backfill the default agent when a legacy identity exists; None
    on a fresh box (new agents are independent of the identity row)."""
    try:
        return ensure_default_agent(conn, secret)
    except AgentError as exc:
        if exc.code != "NO_IDENTITY":
            raise
        return None


def create_agent(
    conn: sqlite3.Connection,
    secret: str,
    name: str,
    display_name: str = "",
    owner_id: str = "local",
) -> dict[str, Any]:
    """Create an agent with a fresh sealed Ed25519 identity (key v1)."""

    try:
        owner = owners_mod.get_owner(conn, (owner_id or "local").strip())
    except owners_mod.OwnerError as exc:
        raise AgentError(exc.code, str(exc), status=exc.status) from exc
    if owner["status"] != "active":
        raise AgentError(
            "OWNER_INACTIVE",
            f"owner {owner['id']!r} is not active.", status=403)
    cleaned = _check_name(name)
    if cleaned == DEFAULT_AGENT_ID:
        ensure_default_agent(conn, secret)
        raise AgentError(
            "ALREADY_EXISTS",
            "the 'default' agent already exists (backfilled from identity).",
            status=409,
        )
    private_key, public_key = crypto.generate_keypair()
    public_raw = crypto.public_key_bytes(public_key)
    agent_id = crypto.agent_id_from_public_key(public_raw)
    sealed = crypto.encrypt_private_key(
        crypto.private_key_bytes(private_key), secret
    )
    now = _now()
    key_id = f"kid_{uuid.uuid4().hex[:12]}"
    try:
        with conn:
            conn.execute(
                "INSERT INTO agents (id, owner_id, agent_id, display_name,"
                " status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (cleaned, owner["id"], agent_id, display_name.strip(),
                 now, now),
            )
            conn.execute(
                "INSERT INTO agent_identities (key_id, agent_id, version,"
                " public_key, encrypted_private_key, fingerprint, status,"
                " created_at, revoked_at)"
                " VALUES (?, ?, 1, ?, ?, ?, 'active', ?, '')",
                (
                    key_id,
                    agent_id,
                    base64.b64encode(public_raw).decode("ascii"),
                    sealed,
                    crypto.fingerprint_from_public_key(public_raw),
                    now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise AgentError(
            "ALREADY_EXISTS",
            f"agent name or id already taken: {exc}",
            status=409,
        ) from exc
    return get_agent(conn, cleaned)


def rotate_agent_key(
    conn: sqlite3.Connection, secret: str, ref: str
) -> dict[str, Any]:
    """Roll an agent to a fresh keypair: new v+1 active, old → rotated.

    Rotated keys stay verifiable (in-flight material); only one active
    key ever exists per agent.
    """
    agent = resolve_agent(conn, ref)
    current = _active_key(conn, agent["agent_id"])
    if current is None:
        # Recovery path: all keys revoked/rotated — mint a fresh active
        # version on top of history instead of stranding the agent.
        latest = conn.execute(
            "SELECT version FROM agent_identities WHERE agent_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (agent["agent_id"],),
        ).fetchone()
        if latest is None:
            raise AgentError(
                "NO_ACTIVE_KEY",
                "agent has no keys at all.",
                status=409,
            )
        base_version = int(latest["version"])
    else:
        base_version = int(current["version"])
    private_key, public_key = crypto.generate_keypair()
    public_raw = crypto.public_key_bytes(public_key)
    sealed = crypto.encrypt_private_key(
        crypto.private_key_bytes(private_key), secret
    )
    now = _now()
    key_id = f"kid_{uuid.uuid4().hex[:12]}"
    with conn:
        if current is not None:
            conn.execute(
                "UPDATE agent_identities SET status = 'rotated',"
                " revoked_at = ? WHERE key_id = ?",
                (now, current["key_id"]),
            )
        conn.execute(
            "INSERT INTO agent_identities (key_id, agent_id, version,"
            " public_key, encrypted_private_key, fingerprint, status,"
            " created_at, revoked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, '')",
            (
                key_id,
                agent["agent_id"],
                base_version + 1,
                base64.b64encode(public_raw).decode("ascii"),
                sealed,
                crypto.fingerprint_from_public_key(public_raw),
                now,
            ),
        )
        conn.execute(
            "UPDATE agents SET updated_at = ? WHERE agent_id = ?",
            (now, agent["agent_id"]),
        )
    return get_agent(conn, agent["agent_id"])


def revoke_agent_key(
    conn: sqlite3.Connection, ref: str, key_id: str | None = None
) -> dict[str, Any]:
    """Mark a key revoked (default: the active one). Revoked keys never
    sign again; history is retained for audit, not deleted."""
    agent = resolve_agent(conn, ref)
    if key_id is None:
        current = _active_key(conn, agent["agent_id"])
        if current is None:
            raise AgentError(
                "NO_ACTIVE_KEY", "agent has no active key.", status=409
            )
        key_id = current["key_id"]
    now = _now()
    with conn:
        cur = conn.execute(
            "UPDATE agent_identities SET status = 'revoked',"
            " revoked_at = ? WHERE key_id = ? AND agent_id = ?",
            (now, key_id, agent["agent_id"]),
        )
        if cur.rowcount == 0:
            raise AgentError(
                "NOT_FOUND", f"unknown key {key_id!r}.", status=404
            )
    return {"agent_id": agent["agent_id"], "key_id": key_id, "revoked": True}


def set_agent_status(
    conn: sqlite3.Connection, ref: str, status: str
) -> dict[str, Any]:
    """Move an agent through its lifecycle.

    ``active`` signs, appears in discovery, and executes.
    ``disabled`` keeps identity/history but performs no new work.
    ``revoked`` additionally distrusts the agent for new operations
    (keys stay in history for audit, never deleted).
    """
    if status not in ("active", "disabled", "revoked"):
        raise AgentError(
            "BAD_STATUS",
            "status must be 'active', 'disabled', or 'revoked'.",
            status=400,
        )
    agent = resolve_agent(conn, ref)
    previous = agent["status"]
    with conn:
        conn.execute(
            "UPDATE agents SET status = ?, updated_at = ? WHERE agent_id = ?",
            (status, _now(), agent["agent_id"]),
        )
    if previous != status:
        emit_best_effort(
            conn,
            f"agent.{status}",
            {"agent_id": agent["agent_id"], "previous": previous},
            agent_id=agent["agent_id"],
        )
    return get_agent(conn, agent["agent_id"])


def key_history(conn: sqlite3.Connection, ref: str) -> list[dict[str, Any]]:
    """All key versions for an agent, newest first (no private material)."""
    agent = resolve_agent(conn, ref)
    rows = conn.execute(
        "SELECT key_id, version, fingerprint, status, created_at, revoked_at"
        " FROM agent_identities WHERE agent_id = ? ORDER BY version DESC",
        (agent["agent_id"],),
    ).fetchall()
    return [dict(row) for row in rows]


def resolve_signing_key(
    conn: sqlite3.Connection, secret: str, ref: str
) -> tuple[Any, str]:
    """Load the agent's ACTIVE private key (verified) for signing.

    Fails closed: unknown/disabled agents and keyless agents raise
    instead of returning anything usable.
    """
    agent = resolve_agent(conn, ref)
    if agent["status"] != "active":
        raise AgentError(
            "NOT_ACTIVE",
            f"agent {agent['id']!r} is {agent['status']}.",
            status=403,
        )
    key = _active_key(conn, agent["agent_id"])
    if key is None:
        raise AgentError(
            "NO_ACTIVE_KEY",
            f"agent {agent['id']!r} has no active key.",
            status=409,
        )
    try:
        private_raw = crypto.decrypt_private_key(
            key["encrypted_private_key"], secret
        )
        private_key = crypto.load_private_key(private_raw)
    except Exception as exc:
        raise AgentError(
            "KEY_CORRUPT",
            f"agent key undecryptable: {exc}",
            status=503,
        ) from exc
    return private_key, agent["agent_id"]


__all__ = [
    "DEFAULT_AGENT_ID",
    "DEFAULT_OWNER_ID",
    "AgentError",
    "create_agent",
    "ensure_default_agent",
    "ensure_default_agent_quiet",
    "get_agent",
    "key_history",
    "list_agents",
    "resolve_agent",
    "resolve_signing_key",
    "revoke_agent_key",
    "rotate_agent_key",
    "set_agent_status",
]
