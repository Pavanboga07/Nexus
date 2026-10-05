"""Full local backup (V8): one JSON doc for the whole laptop state.

Memories export alone is not a backup — pairings, policy rules, and the
local identity fingerprint leave with nothing. This route snapshots all
of them (never any private key material) so a reinstall can restore.
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/backup", tags=["backup"])


def _store():
    from app.memory.store import MemoryStore
    from app.store import migrate, open_db

    path = os.environ.get("NEXUS_DB_PATH", "data/nexus.db")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = open_db(path)
    try:
        migrate(conn)
    finally:
        conn.close()
    return MemoryStore(path), path


def _owned_agent_handles(path: str, owner_id: str) -> list[str]:
    """Agent handles owned by ``owner_id`` (for backup partitioning)."""
    from app.store import open_db

    conn = open_db(path)
    try:
        rows = conn.execute(
            "SELECT id, agent_id FROM agents WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    handles = {str(row["id"] or "").strip() for row in rows}
    handles |= {str(row["agent_id"] or "").strip() for row in rows}
    handles.discard("")
    if owner_id == "local":
        handles.add("default")
    return sorted(handles)


def _owned_memories(store, path: str, owner_id: str) -> list[dict]:
    """Memories from partitions the caller owns (never cross-owner)."""
    memories: list[dict] = []
    seen: set[str] = set()
    for handle in _owned_agent_handles(path, owner_id):
        try:
            rows = store.list_all(agent_id=handle)
        except Exception:
            continue
        for row in rows:
            key = str(row.get("id") or "")
            if key and key not in seen:
                seen.add(key)
                memories.append(row)
    return memories


def _owned_audit(store, memories: list[dict]) -> list[dict]:
    """Audit entries limited to the exported memory ids."""
    try:
        log = store.audit_log()
    except Exception:
        return []
    ids = {str(m.get("id") or "") for m in memories}
    return [e for e in log if str(e.get("memory_id") or "") in ids]


@router.get("")
def backup_route():
    import datetime
    import sqlite3

    from app import pairing
    from app.auth import current_principal
    from app.store import open_db

    owner_id = current_principal().owner_id
    store, path = _store()
    try:
        memories = _owned_memories(store, path, owner_id)
        audit = _owned_audit(store, memories)
    finally:
        store.close()
    conn = open_db(path)
    try:
        try:
            peers = pairing.list_peers(conn)
        except (sqlite3.Error, ValueError):
            peers = []
        try:
            policy = [
                dict(row)
                for row in conn.execute(
                    "SELECT rule_id, peer, data_category, purpose, action,"
                    " effect, created_at FROM policy_rules ORDER BY rowid"
                ).fetchall()
            ]
        except sqlite3.Error:
            policy = []
        identity = None
        try:
            from app.identity.service import ensure_identity
            from app.machine_config import get_or_create_identity_secret

            view = ensure_identity(conn, get_or_create_identity_secret())
            identity = {
                "agent_id": view.agent_id,
                "public_key": view.public_key,
                "fingerprint": view.fingerprint,
            }
        except Exception:  # noqa: BLE001 - backup works without identity
            identity = None
    finally:
        conn.close()
    return JSONResponse(
        content={
            "version": 1,
            "exported_at": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "identity": identity,
            "memories": memories,
            "peers": peers,
            "policy_rules": policy,
            "audit": audit,
        }
    )


__all__ = ["router"]
