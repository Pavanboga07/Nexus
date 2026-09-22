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


@router.get("/export")
def export_route():
    store = _store()
    try:
        return {"memories": store.list_all()}
    finally:
        store.close()


@router.get("")
def list_or_search(q: str | None = None, limit: int = 5):
    store = _store()
    try:
        if q:
            return {"memories": store.recall(q, limit=max(1, min(limit, 50)))}
        return {"memories": store.list_all()}
    finally:
        store.close()


@router.delete("/{memory_id}")
def forget_route(memory_id: str):
    store = _store()
    try:
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
            store.add(
                item["text"],
                session_id=item.get("session_id", "") or "",
                created_at=item.get("created_at", "") or "",
                memory_id=item.get("id"),
            )
            n += 1
        return {"imported": n}
    finally:
        store.close()


__all__ = ["router"]
