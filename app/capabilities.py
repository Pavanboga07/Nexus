"""Agent capabilities: structured, versioned, agent-owned skills.

A capability is ``{agent-handle}.{name}@v{version}`` (e.g.
``research.web_search@1``) with JSON schemas and an optional binding
to a local tool. Capabilities are advertised on signed agent cards
(:func:`agent_card`) and resolved by :mod:`app.discovery` — never by
fuzzy display-name matching.

Only ``active`` capabilities of ``active`` agents execute or appear on
cards; disabled agents cannot mint cards at all (signing resolution
fails closed in :mod:`app.agents`).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

_CAP_NAME_RE = re.compile(r"^[a-z0-9_]{1,48}$")


class CapabilityError(RuntimeError):
    """Capability registry failure with machine ``code`` + HTTP ``status``."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_schema(value: Any, field: str) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, dict):
        raise CapabilityError(
            "BAD_SCHEMA", f"{field} must be a JSON object.", status=400
        )
    try:
        return json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise CapabilityError(
            "BAD_SCHEMA", f"{field} is not JSON-serializable: {exc}",
            status=400,
        ) from exc


def capability_id(handle: str, name: str, version: int) -> str:
    return f"{handle}.{name}@v{version}"


def register_capability(
    conn: sqlite3.Connection,
    agent_ref: str,
    name: str,
    *,
    version: int = 1,
    description: str = "",
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    tool: str | None = None,
) -> dict[str, Any]:
    """Register a versioned capability on an agent's crypto identity."""
    from app import agents

    cleaned = (name or "").strip()
    if not _CAP_NAME_RE.fullmatch(cleaned):
        raise CapabilityError(
            "BAD_NAME",
            "capability name must be 1-48 chars of a-z, 0-9, '_'.",
            status=400,
        )
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CapabilityError(
            "BAD_VERSION", "capability version must be an integer >= 1.",
            status=400,
        )
    agent = agents.resolve_agent(conn, agent_ref)
    bound_tool = None
    if tool is not None:
        from app.agent.tools import build_tools

        bound_tool = tool.strip()
        if bound_tool not in build_tools():
            raise CapabilityError(
                "UNKNOWN_TOOL",
                f"no local tool named {bound_tool!r}.",
                status=400,
            )
    cap_id = capability_id(agent["id"], cleaned, version)
    now = _now()
    try:
        with conn:
            conn.execute(
                "INSERT INTO capabilities (id, agent_id, name, version,"
                " description, input_schema_json, output_schema_json,"
                " tool, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    cap_id,
                    agent["agent_id"],
                    cleaned,
                    version,
                    description.strip(),
                    _check_schema(input_schema, "input_schema"),
                    _check_schema(output_schema, "output_schema"),
                    bound_tool,
                    now,
                    now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise CapabilityError(
            "ALREADY_EXISTS",
            f"capability {cap_id!r} already registered: {exc}",
            status=409,
        ) from exc
    return get_capability(conn, cap_id)


def get_capability(conn: sqlite3.Connection, cap_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, agent_id, name, version, description,"
        " input_schema_json, output_schema_json, tool, status,"
        " created_at, updated_at FROM capabilities WHERE id = ?",
        (cap_id,),
    ).fetchone()
    if row is None:
        raise CapabilityError(
            "NOT_FOUND", f"unknown capability {cap_id!r}.", status=404
        )
    out = dict(row)
    out["input_schema"] = json.loads(out.pop("input_schema_json"))
    out["output_schema"] = json.loads(out.pop("output_schema_json"))
    return out


def list_capabilities(
    conn: sqlite3.Connection,
    agent_ref: str | None = None,
    *,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    from app import agents

    query = (
        "SELECT id, agent_id, name, version, description,"
        " input_schema_json, output_schema_json, tool, status,"
        " created_at, updated_at FROM capabilities"
    )
    params: tuple = ()
    clauses = []
    if agent_ref is not None:
        agent = agents.resolve_agent(conn, agent_ref)
        clauses.append("agent_id = ?")
        params += (agent["agent_id"],)
    if active_only:
        clauses.append("status = 'active'")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id"
    out = []
    for row in conn.execute(query, params).fetchall():
        item = dict(row)
        item["input_schema"] = json.loads(item.pop("input_schema_json"))
        item["output_schema"] = json.loads(item.pop("output_schema_json"))
        out.append(item)
    return out


def set_capability_status(
    conn: sqlite3.Connection, cap_id: str, status: str
) -> dict[str, Any]:
    if status not in ("active", "disabled"):
        raise CapabilityError(
            "BAD_STATUS", "status must be 'active' or 'disabled'.",
            status=400,
        )
    get_capability(conn, cap_id)
    with conn:
        conn.execute(
            "UPDATE capabilities SET status = ?, updated_at = ?"
            " WHERE id = ?",
            (status, _now(), cap_id),
        )
    return get_capability(conn, cap_id)


def agent_card(
    conn: sqlite3.Connection, secret: str, agent_ref: str
) -> dict[str, Any]:
    """Freshly signed agent card advertising active capabilities.

    Answers who (agent_id + fingerprint binding via signature), what
    (capabilities with schemas), where (endpoint), and how to verify
    (public key + signature). Disabled/revoked agents fail closed: key
    resolution refuses to sign.
    """
    from relay.directory import sign_card
    from relay.envelope import utc_iso_in, utc_now_iso

    from app import agents

    agent = agents.resolve_agent(conn, agent_ref)
    private_key, agent_id = agents.resolve_signing_key(
        conn, secret, agent["agent_id"]
    )
    row = conn.execute(
        "SELECT public_key FROM agent_identities WHERE agent_id = ?"
        " AND status = 'active' ORDER BY version DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    if row is None:  # pragma: no cover - resolve_signing_key guards this
        raise CapabilityError(
            "NO_ACTIVE_KEY", "agent has no active key.", status=409
        )
    caps = [
        {
            "id": cap["id"],
            "version": cap["version"],
            "input_schema": cap["input_schema"],
            "output_schema": cap["output_schema"],
        }
        for cap in list_capabilities(conn, agent["agent_id"], active_only=True)
    ]
    card = {
        "type": "agent-card",
        "protocol": "nexus-a2a",
        "version": "0.3",
        "agent_id": agent_id,
        "display_name": agent["display_name"] or agent["id"],
        "public_key": row["public_key"],
        "endpoint": "local",
        "capabilities": caps,
        "supported_purposes": [],
        "issued_at": utc_now_iso(),
        "expires_at": utc_iso_in(30 * 24 * 3600),
    }
    if len(agent["id"]) <= 31:
        card["handle"] = agent["id"]
    return sign_card(private_key, card)


__all__ = [
    "CapabilityError",
    "agent_card",
    "capability_id",
    "get_capability",
    "list_capabilities",
    "register_capability",
    "set_capability_status",
]
