"""Agent discovery: structured resolution, never silent guessing.

Priority (first hit wins, ambiguity returns candidates):

1. exact verified agent ID (local registry, then paired peers)
2. exact verified capability ID
3. exact local handle
4. exact display name
5. substring matching ONLY when ``fuzzy=True`` is explicit

``verified`` is True for locally registered agents (first-party data)
and paired peers (card signature verified at pairing time). Incomplete
gateway-style metadata (id + key + name, no signed card) is reported
as ``UNTRUSTED_METADATA`` and never as verified — no such source is
consulted here; the flag documents the contract for future sources.
"""

from __future__ import annotations

from typing import Any


def _peer_capability_ids(card: Any) -> list[str]:
    """Capability ids advertised on a pinned peer card.

    Supports structured entries (``{"id": ...}``), name-only entries,
    and legacy plain strings. Unsigned/incomplete metadata yields no
    ids here — callers must not treat absence as denial, only as
    "nothing advertised".
    """
    if not isinstance(card, dict):
        return []
    caps = card.get("capabilities")
    if not isinstance(caps, list):
        return []
    out = []
    for entry in caps:
        if isinstance(entry, dict):
            ident = entry.get("id") or entry.get("name")
            if isinstance(ident, str) and ident.strip():
                out.append(ident.strip())
        elif isinstance(entry, str) and entry.strip():
            out.append(entry.strip())
    return out


def _peer_entry(peer: dict[str, Any]) -> dict[str, Any]:
    import json

    card: Any = peer.get("card_json")
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except ValueError:
            card = {}
    return {
        "kind": "peer",
        "id": peer["agent_id"],
        "display_name": peer.get("display_name") or peer["agent_id"],
        "capabilities": _peer_capability_ids(card),
        "verified": True,
    }


def _agent_entry(agent: dict[str, Any], caps: list[dict]) -> dict[str, Any]:
    return {
        "kind": "agent",
        "id": agent["agent_id"],
        "handle": agent["id"],
        "display_name": agent["display_name"] or agent["id"],
        "capabilities": [c["id"] for c in caps],
        "verified": True,
    }


def resolve(
    conn, query: str, *, fuzzy: bool = False
) -> dict[str, Any]:
    """Resolve ``query`` to agent candidates. Never picks silently."""
    from app import agents, capabilities

    text = (query or "").strip()
    if not text:
        return {"matches": [], "explicit": False}
    # Lifecycle gate: only active local agents resolve as targets.
    # Disabled/revoked agents stay in the registry (audit) but are
    # undiscoverable and unusable for new work.
    local = [a for a in agents.list_agents(conn) if a["status"] == "active"]
    peers = [
        dict(row)
        for row in conn.execute(
            "SELECT agent_id, display_name, card_json FROM paired_peers"
        ).fetchall()
        if "card_json" in row.keys()
    ] if _has_card_column(conn) else [
        {"agent_id": r[0], "display_name": r[1]}
        for r in conn.execute(
            "SELECT agent_id, display_name FROM paired_peers"
        ).fetchall()
    ]

    # 1. Exact agent ID (local, then paired).
    for agent in local:
        if agent["agent_id"] == text:
            caps = capabilities.list_capabilities(conn, agent["agent_id"])
            return {
                "matches": [_agent_entry(agent, caps)],
                "explicit": True,
            }
    for peer in peers:
        if peer["agent_id"] == text:
            return {"matches": [_peer_entry(peer)], "explicit": True}

    # 2. Exact capability ID.
    cap_hits = []
    for agent in local:
        ids = [
            c["id"]
            for c in capabilities.list_capabilities(conn, agent["agent_id"])
        ]
        if text in ids:
            cap_hits.append(_agent_entry(
                agent,
                capabilities.list_capabilities(conn, agent["agent_id"]),
            ))
    for peer in peers:
        if text in _peer_entry(peer)["capabilities"]:
            cap_hits.append(_peer_entry(peer))
    if cap_hits:
        return {"matches": cap_hits, "explicit": len(cap_hits) == 1}

    # 3. Exact local handle.
    for agent in local:
        if agent["id"] == text:
            caps = capabilities.list_capabilities(conn, agent["agent_id"])
            return {
                "matches": [_agent_entry(agent, caps)],
                "explicit": True,
            }

    # 4. Exact display name.
    named = []
    for agent in local:
        if (agent["display_name"] or agent["id"]) == text:
            caps = capabilities.list_capabilities(conn, agent["agent_id"])
            named.append(_agent_entry(agent, caps))
    for peer in peers:
        entry = _peer_entry(peer)
        if entry["display_name"] == text:
            named.append(entry)
    if named:
        return {"matches": named, "explicit": len(named) == 1}

    # 5. Substring only when explicitly asked for.
    if fuzzy:
        lowered = text.lower()
        found = []
        for agent in local:
            caps = capabilities.list_capabilities(conn, agent["agent_id"])
            entry = _agent_entry(agent, caps)
            hay = " ".join(
                [entry["display_name"], entry["handle"] or ""]
                + entry["capabilities"]
            ).lower()
            if lowered in hay:
                found.append(entry)
        for peer in peers:
            entry = _peer_entry(peer)
            hay = " ".join(
                [entry["display_name"]] + entry["capabilities"]
            ).lower()
            if lowered in hay:
                found.append(entry)
        return {"matches": found, "explicit": False}
    return {"matches": [], "explicit": False}


def _has_card_column(conn) -> bool:
    return any(
        row["name"] == "card_json"
        for row in conn.execute("PRAGMA table_info(paired_peers)").fetchall()
    )


def advertised_capabilities(conn, agent_id: str) -> list[str]:
    """Capability ids a paired peer advertises on its pinned card."""
    import json

    row = conn.execute(
        "SELECT card_json FROM paired_peers WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        return []
    card = row["card_json"]
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except ValueError:
            return []
    return _peer_capability_ids(card)


__all__ = ["advertised_capabilities", "resolve"]
