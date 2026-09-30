"""Streaming chat endpoint with native search (V5).

``GET /chat/stream?message=...`` runs the bounded tool loop
(max 2 rounds + deterministic digest) against the machine-local
OpenAI-compatible model and streams Server-Sent Events:

- ``token`` — one content delta (first token fast, before completion)
- ``tool_pending`` / ``tool_completed`` — tool cards (purpose-bound)
- ``done`` — final text + citations
- ``error`` — failure code + message

Missing model key → 503 ``MISSING_KEY`` (no settings UI in v1; key via
local machine config — see README).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse

from app.llm.provider import (
    SYSTEM_PROMPT,
    MissingKeyError,
    OpenAICompatibleProvider,
    ProviderRateLimitError,
    ProviderTimeoutError,
    get_llm_config,
)
from app.tools.search_tools import WebFetchTool, WebSearchTool

router = APIRouter(prefix="/chat", tags=["chat"])


def build_provider() -> OpenAICompatibleProvider:
    """Construct the model provider from machine-local config."""
    config = get_llm_config()
    return OpenAICompatibleProvider(
        api_key=config.api_key, model=config.model, base_url=config.base_url
    )


def build_tools() -> dict[str, Any]:
    """Construct the MCP-shaped search tools keyed by name."""
    search = WebSearchTool()
    fetch = WebFetchTool()
    return {search.name: search, fetch.name: fetch}


def tool_schemas(tools: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose only the two search tools in the OpenAI ``tools=`` shape."""
    schemas = []
    for tool in tools.values():
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
        )
    return schemas


async def _execute(
    tools: dict[str, Any], name: str, args: dict[str, Any]
) -> str:
    """Run one tool call; failures arrive as clean error text."""
    tool = tools.get(name)
    if tool is None:
        return f"Unknown tool {name!r}."
    try:
        out = await tool.execute(args)
    except ValueError as exc:
        return f"Invalid arguments for {name}: {exc}"
    return json.dumps(out)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def _events(
    message: str,
    provider: OpenAICompatibleProvider,
    tools: dict[str, Any],
    session_id: str = "",
) -> AsyncIterator[str]:
    messages = [
        {"role": "system", "content": await _system_prompt(message, session_id)},
        {"role": "user", "content": message},
    ]

    async def executor(name: str, args: dict[str, Any]) -> str:
        # Every chat-turn tool call runs purpose-bound as web-research
        # (PURPOSE rides on each tool_pending card event); public-web
        # reads by the owner's own agent need no peer approval.
        return await _execute(tools, name, args)

    assistant_text = ""
    try:
        async for stream_event in provider.stream_with_tools(
            messages, tool_schemas(tools), executor
        ):
            if stream_event.get("type") == "token":
                assistant_text += stream_event.get("text", "")
            elif stream_event.get("type") == "done" and not assistant_text:
                assistant_text = stream_event.get("text", "") or ""
            yield _sse(stream_event)
    except MissingKeyError as exc:
        yield _sse(
            {"type": "error", "code": "MISSING_KEY", "message": str(exc)}
        )
    except (ProviderTimeoutError, ProviderRateLimitError) as exc:
        yield _sse({"type": "error", "code": exc.code, "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - SSE must stay well-formed
        yield _sse({"type": "error", "code": "STREAM_FAILED", "message": str(exc)})
    finally:
        _extract_after_turn(message, assistant_text, session_id)


def _recall_sync(db_path: str, message: str, session_id: str) -> list[dict]:
    """Blocking recall on a FRESH store — runs in a worker thread only.

    Never shares the request-thread store: the worker opens its own
    ``MemoryStore`` and closes it before returning.
    """
    from app.memory.store import MemoryStore

    store = MemoryStore(db_path)
    try:
        return store.recall(message, limit=5, session_id=session_id)
    finally:
        store.close()


async def _system_prompt(message: str, session_id: str) -> str:
    """System prompt with session-scoped recall injected (best-effort).

    Memories stay scoped per session: only rows stored under this
    ``session_id`` are injected. Recall runs in a worker thread with a
    1.0s bound so slow sqlite-vec/embed work never holds first token —
    any timeout or failure falls back to the bare prompt.
    """
    import os

    db_path = os.environ.get("NEXUS_DB_PATH", "data/nexus.db")
    try:
        loop = asyncio.get_running_loop()
        hits = await asyncio.wait_for(
            loop.run_in_executor(None, _recall_sync, db_path, message, session_id),
            timeout=1.0,
        )
    except Exception:  # noqa: BLE001 - recall must not break chat
        return SYSTEM_PROMPT
    if not hits:
        return SYSTEM_PROMPT
    lines = "\n".join(f"- {hit['text']}" for hit in hits)
    return f"{SYSTEM_PROMPT}\n\nSession memories (use them when relevant):\n{lines}"


def _extract_after_turn(
    user_text: str, assistant_text: str, session_id: str = ""
) -> None:
    """Fire-and-forget memory extraction; never breaks the chat turn."""
    try:
        import datetime
        import os

        from app.memory.extract import queue_extraction

        queue_extraction(
            os.environ.get("NEXUS_DB_PATH", "data/nexus.db"),
            user_text=user_text,
            assistant_text=assistant_text,
            session_id=session_id,
            created_at=datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
        )
    except Exception:  # noqa: BLE001 - extraction must not break chat
        pass


@router.get("/stream")
async def chat_stream(
    message: str = Query(min_length=1, max_length=4000),
    session_id: str = Query(default="", max_length=200),
):
    try:
        provider = build_provider()
    except MissingKeyError as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "code": "MISSING_KEY"},
        )
    tools = build_tools()
    return StreamingResponse(
        _events(message, provider, tools, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["build_provider", "build_tools", "router", "tool_schemas"]
