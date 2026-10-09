"""Turn context: recall, thread history, persistence, extraction.

All SQLite work opens a fresh migrated connection per call (same idiom
as the routes) so nodes stay thread-safe under the event loop.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import uuid
from typing import Any


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


from app.store import db_path  # canonical NEXUS_DB_PATH helper (finding F8)


def _conn():
    from app.store import migrate, open_db

    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = open_db(path)
    migrate(conn)
    return conn


def thread_title(message: str) -> str:
    flat = " ".join(message.split())
    if not flat:
        return "New chat"
    return flat[:57] + "..." if len(flat) > 60 else flat


def save_turn(
    thread_id: str,
    role: str,
    text: str,
    citations: list[str] | None = None,
    agent_id: str = "default",
) -> None:
    """Append one turn; create the thread row (titled from the first
    user message, owned by the acting agent) or bump ``updated_at``."""
    conn = _conn()
    try:
        now = utcnow()
        owner = agent_id or "default"
        row = conn.execute(
            "SELECT thread_id, agent_id FROM chat_threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            title = thread_title(text) if role == "user" else "New chat"
            conn.execute(
                "INSERT INTO chat_threads"
                " (thread_id, title, created_at, updated_at, agent_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (thread_id, title, now, now, owner),
            )
        else:
            conn.execute(
                "UPDATE chat_threads SET updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
        conn.execute(
            "INSERT INTO chat_turns"
            " (turn_id, thread_id, role, text, citations_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                thread_id,
                role,
                text,
                json.dumps(citations or []),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_turns(thread_id: str, limit: int = 10,
               agent_id: str | None = None) -> list[dict[str, Any]]:
    """Most recent turns, oldest-first, capped for prompt context.

    With ``agent_id`` set, turns load only when the thread belongs to
    that agent; cross-agent reads return empty.
    """
    conn = _conn()
    try:
        if agent_id is None:
            rows = conn.execute(
                "SELECT role, text, citations_json, created_at"
                " FROM chat_turns WHERE thread_id = ?"
                " ORDER BY rowid DESC LIMIT ?",
                (thread_id, max(1, limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.role, t.text, t.citations_json, t.created_at"
                " FROM chat_turns t JOIN chat_threads h"
                " ON h.thread_id = t.thread_id"
                " WHERE t.thread_id = ? AND h.agent_id = ?"
                " ORDER BY t.rowid DESC LIMIT ?",
                (thread_id, agent_id, max(1, limit)),
            ).fetchall()
    finally:
        conn.close()
    out = []
    for row in reversed(rows):
        try:
            citations = json.loads(row["citations_json"] or "[]")
        except (json.JSONDecodeError, ValueError):
            citations = []
        out.append(
            {
                "role": row["role"],
                "text": row["text"],
                "citations": citations,
                "created_at": row["created_at"],
            }
        )
    return out


def list_threads(limit: int = 50,
                 agent_id: str | None = None) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        if agent_id is None:
            rows = conn.execute(
                "SELECT thread_id, title, updated_at, agent_id"
                " FROM chat_threads ORDER BY updated_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT thread_id, title, updated_at, agent_id"
                " FROM chat_threads WHERE agent_id = ?"
                " ORDER BY updated_at DESC LIMIT ?",
                (agent_id, max(1, limit)),
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def thread_owner(thread_id: str) -> str | None:
    """Owning agent handle of a thread (None when unknown)."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT agent_id FROM chat_threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return str(row["agent_id"] or "default")


def delete_thread(thread_id: str) -> bool:
    conn = _conn()
    try:
        with conn:
            conn.execute(
                "DELETE FROM chat_turns WHERE thread_id = ?", (thread_id,)
            )
            cur = conn.execute(
                "DELETE FROM chat_threads WHERE thread_id = ?", (thread_id,)
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def recall_sync(message: str, session_id: str,
                agent_id: str = "default") -> list[dict]:
    """Blocking recall on a FRESH store — runs in a worker thread only.

    Never shares the request-thread store: the worker opens its own
    ``MemoryStore`` and closes it before returning. Recall is scoped to
    the acting agent's memory partition.
    """
    from app.memory.store import MemoryStore

    store = MemoryStore(db_path())
    try:
        return store.recall(message, limit=5, session_id=session_id,
                            agent_id=agent_id or "default")
    finally:
        store.close()


async def system_prompt(message: str, session_id: str,
                        agent_id: str = "default") -> str:
    """System prompt with session-scoped recall injected (best-effort).

    Memories stay scoped per session AND per agent: only rows stored
    under this ``session_id`` in the acting agent's partition are
    injected. Recall runs in a worker thread with a 1.0s bound so slow
    sqlite-vec/embed work never holds first token — any timeout or
    failure falls back to the bare prompt.
    """
    from app.llm.provider import SYSTEM_PROMPT

    owner = agent_id or "default"
    try:
        loop = asyncio.get_running_loop()
        hits = await asyncio.wait_for(
            loop.run_in_executor(
                None, recall_sync, message, session_id, owner),
            timeout=1.0,
        )
    except Exception:  # noqa: BLE001 - recall must not break chat
        return SYSTEM_PROMPT
    if not hits:
        return SYSTEM_PROMPT
    lines = "\n".join(f"- {hit['text']}" for hit in hits)
    return f"{SYSTEM_PROMPT}\n\nSession memories (use them when relevant):\n{lines}"


def extract_after_turn(
    user_text: str, assistant_text: str, session_id: str = "",
    agent_id: str = "default",
) -> None:
    """Fire-and-forget memory extraction; never breaks the chat turn."""
    try:
        from app.memory.extract import queue_extraction

        queue_extraction(
            db_path(),
            user_text=user_text,
            assistant_text=assistant_text,
            session_id=session_id,
            created_at=utcnow(),
            agent_id=agent_id or "default",
        )
    except Exception:  # noqa: BLE001 - extraction must not break chat
        pass


__all__ = [
    "db_path",
    "delete_thread",
    "extract_after_turn",
    "list_threads",
    "load_turns",
    "recall_sync",
    "save_turn",
    "system_prompt",
    "thread_title",
    "utcnow",
]
