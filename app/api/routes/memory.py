"""Memory browser API (V6): thin glue over ``app.memory``.

Config from the environment at request time (test-friendly):
``NEXUS_DB_PATH`` (SQLite file, shared with the rest of the laptop
store — memory tables live alongside it).
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/memory", tags=["memory"])


class ImportIn(BaseModel):
    memories: list[dict]


class UpdateIn(BaseModel):
    text: str


class BulkDeleteIn(BaseModel):
    ids: list[str]


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
    return MemoryStore(path)


class _OwnerDenied(RuntimeError):
    """Cross-owner (or unknown-agent) access: surfaced as 404."""


def _owned_agent(store, ref: str) -> str:
    """Agent handle the caller owns; unknown-or-foreign → denial.

    The unregistered "default" partition is readable only by the local
    owner (legacy rows predate the registry); everyone else must use a
    registered agent of their own.
    """
    from app import agents
    from app.auth import current_principal

    owner = current_principal().owner_id
    try:
        agent = agents.resolve_agent(store._conn, ref or "default")
    except agents.AgentError:
        if (ref or "default") == "default" and owner == "local":
            return "default"
        raise _OwnerDenied()
    if agent["owner_id"] != owner:
        raise _OwnerDenied()
    return agent["id"]


def _row_owner(store, memory_id: str) -> str:
    row = store._conn.execute(
        "SELECT agent_id FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    return str(row["agent_id"]) if row is not None else ""


def _denied():
    return JSONResponse(
        status_code=404,
        content={"detail": "Not found.", "code": "NOT_FOUND"},
    )


@router.get("/export")
def export_route(agent: str = "default"):
    store = _store()
    try:
        try:
            handle = _owned_agent(store, agent)
        except _OwnerDenied:
            return _denied()
        return {"memories": store.list_all(agent_id=handle)}
    finally:
        store.close()


@router.get("")
def list_or_search(
    q: str | None = None, limit: int = 5, agent: str = "default"
):
    store = _store()
    try:
        try:
            handle = _owned_agent(store, agent)
        except _OwnerDenied:
            return _denied()
        if q:
            return {
                "memories": store.recall(
                    q, limit=max(1, min(limit, 50)), agent_id=handle
                )
            }
        return {"memories": store.list_all(agent_id=handle)}
    finally:
        store.close()


@router.get("/audit")
def audit_route(limit: int = 100):
    """Append-only forget/edit log (never the forgotten text), newest first."""
    from app.auth import current_principal

    store = _store()
    try:
        log = store.audit_log()
        window = log[-max(1, min(limit, 500)) :]
        window.reverse()
        mine = []
        for entry in window:
            owner = _row_owner(store, entry.get("memory_id", ""))
            if not owner:
                # Orphaned by forget: visible to local owner only.
                if current_principal().owner_id == "local":
                    mine.append(entry)
            elif _row_visible(store, entry["memory_id"]):
                mine.append(entry)
        return {"audit": mine}
    finally:
        store.close()


def _row_visible(store, memory_id: str) -> bool:
    """Row exists and belongs to one of the caller's agents."""
    from app.auth import current_principal

    owner = _row_owner(store, memory_id)
    if not owner:
        return False
    if owner == "default":
        if current_principal().owner_id != "local":
            return False
        return True
    try:
        _owned_agent(store, owner)
        return True
    except _OwnerDenied:
        return False


@router.put("/{memory_id}")
def update_route(memory_id: str, body: UpdateIn):
    text = (body.text or "").strip()
    if not text:
        return JSONResponse(
            status_code=400,
            content={"detail": "text must not be empty", "code": "BAD_UPDATE"},
        )
    store = _store()
    try:
        if not _row_visible(store, memory_id):
            return JSONResponse(
                status_code=404,
                content={"detail": "unknown memory", "code": "NOT_FOUND"},
            )
        if not store.update(memory_id, text):
            return JSONResponse(
                status_code=404,
                content={"detail": "unknown memory", "code": "NOT_FOUND"},
            )
        return {"id": memory_id, "updated": True}
    finally:
        store.close()


@router.post("/bulk-delete")
def bulk_delete_route(body: BulkDeleteIn):
    store = _store()
    try:
        forgotten = 0
        missing = []
        for memory_id in body.ids:
            if not _row_visible(store, memory_id):
                missing.append(memory_id)
            elif store.forget(memory_id):
                forgotten += 1
            else:
                missing.append(memory_id)
        return {"forgotten": forgotten, "missing": missing}
    finally:
        store.close()


@router.delete("/{memory_id}")
def forget_route(memory_id: str):
    store = _store()
    try:
        if not _row_visible(store, memory_id):
            return JSONResponse(
                status_code=404,
                content={"detail": "unknown memory", "code": "NOT_FOUND"},
            )
        if not store.forget(memory_id):
            return JSONResponse(
                status_code=404,
                content={"detail": "unknown memory", "code": "NOT_FOUND"},
            )
        return {"id": memory_id, "forgotten": True}
    finally:
        store.close()


@router.post("/import")
def import_route(body: ImportIn):
    store = _store()
    try:
        n = 0
        for item in body.memories:
            if not isinstance(item, dict) or "text" not in item:
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": "each memory needs 'text'",
                        "code": "BAD_IMPORT",
                    },
                )
            try:
                owner = _owned_agent(
                    store, item.get("agent_id", "") or "default")
            except _OwnerDenied:
                return _denied()
            store.add(
                item["text"],
                session_id=item.get("session_id", "") or "",
                created_at=item.get("created_at", "") or "",
                memory_id=item.get("id"),
                agent_id=owner,
            )
            n += 1
        return {"imported": n}
    finally:
        store.close()


__all__ = ["router"]
