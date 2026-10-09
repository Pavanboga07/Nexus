"""Owner registry + operator token issuance (multi-owner data model).

One Nexus deployment serves one operator today, but every data row
already carries an owner boundary and this module makes it real:
owners are first-class rows, bearer tokens map to owners with scopes,
and revocation is immediate. The legacy file/env token keeps working
as the implicit `local`-owner full operator credential (backward
compatible bootstrap).

Token plaintext exists only at creation (returned once) and in
transit; at rest only SHA-256 hashes are stored. Hashes are never
returned by any API.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from app.errors import NexusError

_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
DEFAULT_OWNER = "local"
OPERATOR_SCOPE = "operator"


class OwnerError(NexusError):
    """Owner/token failure with machine ``code`` + HTTP ``status``."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_local_owner(conn: sqlite3.Connection) -> dict[str, Any]:
    """Backfill the default owner (migration seeds it; this is belt
    and braces for direct service use)."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO owners (id, display_name, status,"
            " created_at) VALUES ('local', 'Local operator', 'active', ?)",
            (_now(),),
        )
    return get_owner(conn, DEFAULT_OWNER)


def get_owner(conn: sqlite3.Connection, owner_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, display_name, status, created_at FROM owners"
        " WHERE id = ?",
        (owner_id,),
    ).fetchone()
    if row is None:
        raise OwnerError("NOT_FOUND", f"unknown owner {owner_id!r}.",
                         status=404)
    return dict(row)


def list_owners(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, display_name, status, created_at FROM owners"
        " ORDER BY id").fetchall()]


def create_owner(conn: sqlite3.Connection, owner_id: str,
                 display_name: str = "") -> dict[str, Any]:
    cleaned = (owner_id or "").strip().lower()
    if not _OWNER_RE.fullmatch(cleaned):
        raise OwnerError(
            "BAD_OWNER",
            "owner id must be 1-64 chars of a-z, 0-9, '-' (start/end"
            " alnum).",
            status=400,
        )
    if cleaned == DEFAULT_OWNER:
        return ensure_local_owner(conn)
    now = _now()
    try:
        with conn:
            conn.execute(
                "INSERT INTO owners (id, display_name, status,"
                " created_at) VALUES (?, ?, 'active', ?)",
                (cleaned, (display_name or "").strip()[:200], now),
            )
    except sqlite3.IntegrityError as exc:
        raise OwnerError(
            "ALREADY_EXISTS", f"owner {cleaned!r} exists: {exc}",
            status=409,
        ) from exc
    return get_owner(conn, cleaned)


def set_owner_status(conn: sqlite3.Connection, owner_id: str,
                     status: str) -> dict[str, Any]:
    if status not in ("active", "disabled"):
        raise OwnerError(
            "BAD_STATUS", "status must be 'active' or 'disabled'.",
            status=400)
    owner = get_owner(conn, owner_id)
    if owner_id == DEFAULT_OWNER and status != "active":
        raise OwnerError(
            "PROTECTED_OWNER",
            "the local owner cannot be disabled.", status=409)
    with conn:
        conn.execute(
            "UPDATE owners SET status = ? WHERE id = ?",
            (status, owner_id),
        )
    return get_owner(conn, owner_id)


def issue_token(conn: sqlite3.Connection, owner_id: str,
                scopes: str = OPERATOR_SCOPE) -> dict[str, Any]:
    """Mint a bearer token; plaintext returned ONCE, hash stored."""
    owner = get_owner(conn, owner_id)
    if owner["status"] != "active":
        raise OwnerError(
            "OWNER_INACTIVE", f"owner {owner_id!r} is not active.",
            status=403)
    clean_scopes = (scopes or "").strip() or OPERATOR_SCOPE
    raw = f"nx_{secrets.token_urlsafe(32)}"
    token_id = f"tok_{uuid.uuid4().hex[:12]}"
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO operator_tokens (id, owner_id, token_hash,"
            " scopes, created_at, revoked_at)"
            " VALUES (?, ?, ?, ?, ?, '')",
            (token_id, owner_id, _hash_token(raw), clean_scopes, now),
        )
    return {"id": token_id, "owner_id": owner_id, "scopes": clean_scopes,
            "token": raw, "created_at": now}


def resolve_token(conn: sqlite3.Connection,
                  token: str) -> dict[str, Any] | None:
    """Map a DB bearer token to (owner, scopes); None when unknown,
    revoked, or owner-disabled. Never raises on bad input."""
    if not token:
        return None
    row = conn.execute(
        "SELECT t.scopes, t.revoked_at, o.id AS owner_id, o.status"
        " FROM operator_tokens t JOIN owners o ON o.id = t.owner_id"
        " WHERE t.token_hash = ?",
        (_hash_token(token),),
    ).fetchone()
    if row is None or row["revoked_at"] or row["status"] != "active":
        return None
    return {"owner_id": row["owner_id"],
            "scopes": tuple(
                s for s in str(row["scopes"] or "").split(",") if s)}


def revoke_token(conn: sqlite3.Connection, token_id: str) -> dict:
    row = conn.execute(
        "SELECT id FROM operator_tokens WHERE id = ?", (token_id,)
    ).fetchone()
    if row is None:
        raise OwnerError("NOT_FOUND", f"unknown token {token_id!r}.",
                         status=404)
    with conn:
        conn.execute(
            "UPDATE operator_tokens SET revoked_at = ? WHERE id = ?",
            (_now(), token_id),
        )
    return {"id": token_id, "revoked": True}


def list_tokens(conn: sqlite3.Connection,
                owner_id: str) -> list[dict[str, Any]]:
    get_owner(conn, owner_id)
    return [dict(r) for r in conn.execute(
        "SELECT id, owner_id, scopes, created_at, revoked_at"
        " FROM operator_tokens WHERE owner_id = ? ORDER BY created_at ASC",
        (owner_id,)).fetchall()]


__all__ = [
    "DEFAULT_OWNER",
    "OPERATOR_SCOPE",
    "OwnerError",
    "create_owner",
    "ensure_local_owner",
    "get_owner",
    "issue_token",
    "list_owners",
    "list_tokens",
    "resolve_token",
    "revoke_token",
    "set_owner_status",
]
