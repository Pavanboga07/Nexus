"""Streaming chat endpoint: agent graph (V8) over native search (V5).

``GET /chat/stream?message=...`` runs one LangGraph turn
(load_context -> respond -> persist -> extract) against the
machine-local OpenAI-compatible model and streams Server-Sent Events:

- ``token`` — one content delta (first token fast, before completion)
- ``tool_pending`` / ``tool_completed`` — tool cards (purpose-bound)
- ``done`` — final text + citations
- ``error`` — failure code + message

Turns persist per ``session_id`` (the thread key) with multi-turn
history loaded into every prompt:

- ``GET /chat/threads`` — thread list, newest first
- ``GET /chat/threads/{id}`` — one thread with its turns
- ``DELETE /chat/threads/{id}`` — delete a thread and its turns

Missing model key → 503 ``MISSING_KEY`` (paste the key in the UI
under Chat settings, or set it via local machine config — see README).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse

from app.agent.graph import build_graph, run_turn
from app.agent.tools import build_provider, build_tools, tool_schemas
from app.llm.provider import MissingKeyError

router = APIRouter(prefix="/chat", tags=["chat"])


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _checkpoint_path() -> str:
    from app.agent.context import db_path

    base = os.path.dirname(db_path())
    if base:
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "graph_checkpoints.db")
    return "graph_checkpoints.db"


def _my_agent_ids() -> set[str]:
    """Handles + crypto ids of agents the caller owns (for thread scoping)."""
    from app import agents
    from app.auth import current_principal

    conn = _agent_conn()
    try:
        try:
            rows = agents.list_agents(conn, owner_id=current_principal().owner_id)
        except Exception:
            return set()
        ids: set[str] = set()
        for row in rows:
            for key in ("id", "agent_id"):
                value = str(row.get(key) or "").strip()
                if value:
                    ids.add(value)
        return ids
    finally:
        conn.close()


def _resolve_chat_agent(agent_ref: str) -> dict[str, Any]:
    """Resolve the acting agent for a chat turn (default agent fallback).

    Unknown names fail with candidates instead of silently chatting as
    someone else; the default agent stays the default starting actor —
    on a fresh box with no registry yet, "default" resolves implicitly
    so first-run chat works before any agent exists.

    Cross-owner agents are never returned: a foreign handle looks
    exactly like an unknown one.
    """
    from app import agents, discovery
    from app.auth import current_principal

    ref = (agent_ref or "").strip() or "default"
    mine = current_principal().owner_id
    conn = _agent_conn()
    try:
        try:
            agent = agents.get_agent(conn, ref)
            if agent.get("owner_id") != mine:
                raise _ChatAgentError(ref, [])
            return agent
        except agents.AgentError:
            pass
        found = discovery.resolve(conn, ref, fuzzy=False)
        if found["explicit"] and found["matches"]:
            match = found["matches"][0]
            if match["kind"] == "agent":
                try:
                    raw = agents.resolve_agent(conn, match["id"])
                except agents.AgentError:
                    raise _ChatAgentError(ref, found["matches"])
                if raw["owner_id"] != mine:
                    raise _ChatAgentError(ref, [])
                return agents.get_agent(conn, match["id"])
        if ref == "default":
            return {"id": "default", "agent_id": "default"}
        raise _ChatAgentError(ref, found["matches"])
    finally:
        conn.close()


class _ChatAgentError(ValueError):
    def __init__(self, ref: str, candidates: list) -> None:
        self.ref = ref
        self.candidates = [
            {"id": m["id"], "display_name": m.get("display_name", "")}
            for m in candidates
        ]
        names = ", ".join(m["id"] for m in self.candidates[:5])
        super().__init__(
            f"unknown agent {ref!r}."
            + (f" Did you mean: {names}?" if names else "")
        )


def _agent_conn():
    from app.agent.context import db_path
    from app.store import migrate, open_db

    conn = open_db(db_path())
    migrate(conn)
    return conn


async def _run_graph(
    message: str,
    session_id: str,
    provider: Any,
    tools: dict[str, Any],
    queue: asyncio.Queue,
    agent_id: str = "default",
) -> None:
    """Run one graph turn; provider events land on ``queue`` + sentinel."""
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with AsyncSqliteSaver.from_conn_string(
            _checkpoint_path()
        ) as checkpointer:
            graph = build_graph(checkpointer)
            await run_turn(
                graph,
                thread_id=session_id or "default",
                message=message,
                provider=provider,
                tools=tools,
                sink=queue,
                agent_id=agent_id,
            )
    except Exception as exc:  # noqa: BLE001 - SSE must stay well-formed
        queue.put_nowait(
            {"type": "error", "code": "STREAM_FAILED", "message": str(exc)}
        )
    finally:
        queue.put_nowait(None)


async def _graph_events(
    message: str,
    session_id: str,
    provider: Any,
    tools: dict[str, Any],
    agent: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    from app import tasks as task_tracker

    acting = agent or {"id": "default", "agent_id": "default"}
    conn = _agent_conn()
    turn = None
    try:
        turn = task_tracker.create_task(
            conn,
            requesting_agent_id="owner",
            target_agent=acting.get("agent_id") or "default",
            task_input={"question": message[:500]},
            purpose="answer",
        )
        for state in ("RESOLVING", "AUTHORIZED", "DISPATCHED", "RUNNING"):
            try:
                turn = task_tracker.transition(conn, turn["task_id"], state)
            except task_tracker.TaskError:
                break
    finally:
        conn.close()
    queue: asyncio.Queue = asyncio.Queue()
    # Recall/extraction partition on the stable local handle.
    partition = acting.get("id") or "default"
    task = asyncio.create_task(
        _run_graph(message, session_id, provider, tools, queue, partition)
    )
    failed = False
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            if isinstance(event, dict) and event.get("type") == "error":
                failed = True
            yield _sse(event)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if turn is not None:
            done_conn = _agent_conn()
            try:
                if failed:
                    try:
                        task_tracker.fail_task(
                            done_conn, turn["task_id"], "STREAM_FAILED",
                            "chat turn stream errored")
                    except task_tracker.TaskError:
                        pass
                else:
                    try:
                        task_tracker.complete_task(
                            done_conn, turn["task_id"],
                            {"thread_id": session_id or "default"})
                    except task_tracker.TaskError:
                        pass
            finally:
                done_conn.close()


def _agent_is_mine(agent_ref: str) -> bool:
    """Agent exists and belongs to the caller (unknown counts as no)."""
    from app import agents
    from app.agent.context import db_path
    from app.auth import current_principal
    from app.store import migrate, open_db

    conn = open_db(db_path())
    migrate(conn)
    try:
        row = agents.resolve_agent(conn, agent_ref)
        return row["owner_id"] == current_principal().owner_id
    except agents.AgentError:
        return False
    finally:
        conn.close()


@router.get("/threads")
def list_chat_threads(agent: str | None = None):
    from app.agent.context import list_threads

    if agent is not None:
        if not _agent_is_mine(agent):
            return {"threads": []}
        return {"threads": list_threads(agent_id=agent)}
    # No agent filter: scope to the caller's own agents (never all owners).
    mine = _my_agent_ids() | {"default"}
    visible = [
        item for item in list_threads(limit=1000)
        if str(item.get("agent_id") or "default") in mine
    ]
    return {"threads": visible[:50]}


@router.get("/threads/{thread_id}")
def get_chat_thread(thread_id: str, agent: str | None = None):
    from app.agent.context import list_threads, load_turns, thread_owner

    meta = next(
        (
            item
            for item in list_threads(limit=1000)
            if item["thread_id"] == thread_id
        ),
        None,
    )
    if meta is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "unknown thread", "code": "NOT_FOUND"},
        )
    owner = thread_owner(thread_id) or "default"
    if agent is not None:
        if owner != agent:
            return JSONResponse(
                status_code=403,
                content={"detail": "thread belongs to another agent.",
                         "code": "FORBIDDEN"},
            )
        if not _agent_is_mine(agent):
            return JSONResponse(
                status_code=403,
                content={"detail": "thread belongs to another agent.",
                         "code": "FORBIDDEN"},
            )
    elif owner not in _my_agent_ids() | {"default"}:
        # Cross-owner thread: indistinguishable from unknown.
        return JSONResponse(
            status_code=404,
            content={"detail": "unknown thread", "code": "NOT_FOUND"},
        )
    return {
        "thread_id": thread_id,
        "title": meta["title"],
        "turns": load_turns(thread_id, limit=200,
                            agent_id=agent or None),
    }


@router.delete("/threads/{thread_id}")
def delete_chat_thread(thread_id: str):
    from app.agent.context import delete_thread, thread_owner

    owner = thread_owner(thread_id)
    if owner is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "unknown thread", "code": "NOT_FOUND"},
        )
    if owner not in _my_agent_ids() | {"default"}:
        return JSONResponse(
            status_code=404,
            content={"detail": "unknown thread", "code": "NOT_FOUND"},
        )
    if not delete_thread(thread_id):
        return JSONResponse(
            status_code=404,
            content={"detail": "unknown thread", "code": "NOT_FOUND"},
        )
    return {"id": thread_id, "deleted": True}


@router.get("/stream")
async def chat_stream(
    message: str = Query(min_length=1, max_length=4000),
    session_id: str = Query(default="", max_length=200),
    agent: str = Query(default="default", max_length=200),
):
    try:
        provider = build_provider()
    except MissingKeyError as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "code": "MISSING_KEY"},
        )
    try:
        acting = _resolve_chat_agent(agent)
    except _ChatAgentError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "code": "UNKNOWN_AGENT",
                     "candidates": exc.candidates},
        )
    tools = build_tools()
    return StreamingResponse(
        _graph_events(message, session_id, provider, tools, acting),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["build_provider", "build_tools", "router", "tool_schemas"]
