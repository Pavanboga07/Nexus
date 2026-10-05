"""Conversational agent graph: load_context -> respond -> persist -> extract.

The hardened provider loop (``stream_with_tools``) is the execution
engine — untouched, fully tested. The graph orchestrates it:

- ``load_context`` — session memories into the system prompt plus the
  thread's recent turns, so every turn is a real multi-turn reply
  (previously each turn sent system + one message, stateless).
- ``respond`` — runs the provider loop with full history and forwards
  its token/tool/done events into a per-run queue carried in
  ``config["configurable"]["sink"]``; the SSE layer drains the queue.
  Provider + tools also ride the config so tests can inject fakes.
- ``persist`` — appends the user + assistant turns (thread row is
  created on first turn, titled from the first message).
- ``extract`` — fire-and-forget memory extraction, same as before.

Pass a checkpointer to ``build_graph`` for durable per-thread state
(the route uses a file-backed SQLite saver; tests use ``MemorySaver``
or none). ``config["configurable"]["thread_id"]`` scopes the
checkpoint, matching the thread key.
"""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

_HISTORY_TURNS = 10


class TurnState(TypedDict, total=False):
    thread_id: str
    message: str
    system: str
    history: list[dict[str, Any]]
    tool_texts: Annotated[list[str], operator.add]
    citations: list[str]
    assistant_text: str
    done_text: str


def _cfg(config: Any) -> dict[str, Any]:
    if isinstance(config, dict):
        nested = config.get("configurable")
        if isinstance(nested, dict):
            return nested
    return {}


def _emit(sink: Any, event: dict[str, Any]) -> None:
    if sink is not None:
        sink.put_nowait(event)


async def load_context(
    state: TurnState, config: RunnableConfig
) -> dict[str, Any]:
    """Recall session memories + recent thread turns for the prompt."""
    from app.agent.context import load_turns, system_prompt

    thread_id = state.get("thread_id", "")
    message = state.get("message", "")
    agent_id = _cfg(config).get("agent_id", "default") or "default"
    system = await system_prompt(message, thread_id, agent_id=agent_id)
    agent_id = _cfg(config).get("agent_id", "default") or "default"
    turns = await asyncio.to_thread(
        load_turns, thread_id, _HISTORY_TURNS, agent_id)
    history = [
        {
            "role": "assistant" if turn["role"] == "assistant" else "user",
            "content": turn["text"],
        }
        for turn in turns
    ]
    return {"system": system, "history": history}


async def respond(
    state: TurnState, config: RunnableConfig
) -> dict[str, Any]:
    """Run one provider turn with full history; forward events to sink."""
    from app.agent.tools import make_executor, tool_schemas
    from app.llm.provider import (
        MissingKeyError,
        ProviderRateLimitError,
        ProviderTimeoutError,
    )

    cfg = _cfg(config)
    provider = cfg.get("provider")
    tools = cfg.get("tools") or {}
    sink = cfg.get("sink")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": state.get("system", "")},
        *state.get("history", []),
        {"role": "user", "content": state.get("message", "")},
    ]
    assistant_text = ""
    done_text = ""
    citations: list[str] = []
    try:
        async for event in provider.stream_with_tools(
            messages, tool_schemas(tools), make_executor(tools)
        ):
            _emit(sink, dict(event))
            kind = event.get("type")
            if kind == "token":
                assistant_text += str(event.get("text", ""))
            elif kind == "done":
                done_text = str(event.get("text", "")) or assistant_text
                raw = event.get("citations", []) or []
                citations = [str(url) for url in raw]
                if not done_text:
                    done_text = assistant_text
    except MissingKeyError as exc:
        _emit(sink, {"type": "error", "code": "MISSING_KEY", "message": str(exc)})
    except (ProviderTimeoutError, ProviderRateLimitError) as exc:
        _emit(sink, {"type": "error", "code": exc.code, "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - SSE must stay well-formed
        _emit(
            sink,
            {"type": "error", "code": "STREAM_FAILED", "message": str(exc)},
        )
    return {
        "assistant_text": assistant_text,
        "citations": citations,
        "done_text": done_text or assistant_text,
    }


async def persist(
    state: TurnState, config: RunnableConfig
) -> dict[str, Any]:
    """Append the user + assistant turns to the agent-owned thread."""
    from app.agent.context import save_turn

    thread_id = state.get("thread_id", "")
    message = state.get("message", "")
    text = state.get("done_text") or state.get("assistant_text", "")
    citations = state.get("citations", [])
    owner = _cfg(config).get("agent_id", "default") or "default"
    if message:
        await asyncio.to_thread(
            save_turn, thread_id, "user", message, [], owner)
    if text:
        await asyncio.to_thread(
            save_turn, thread_id, "assistant", text, citations, owner)
    return {}


async def extract(
    state: TurnState, config: RunnableConfig
) -> dict[str, Any]:
    """Queue memory extraction for the finished turn."""
    from app.agent.context import extract_after_turn

    extract_after_turn(
        state.get("message", ""),
        state.get("done_text") or state.get("assistant_text", ""),
        state.get("thread_id", ""),
        agent_id=_cfg(config).get("agent_id", "default") or "default",
    )
    return {}


def build_graph(checkpointer: Any = None):
    """Compile the turn graph, optionally with a checkpointer."""
    graph = StateGraph(TurnState)
    graph.add_node("load_context", load_context)
    graph.add_node("respond", respond)
    graph.add_node("persist", persist)
    graph.add_node("extract", extract)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "respond")
    graph.add_edge("respond", "persist")
    graph.add_edge("persist", "extract")
    graph.add_edge("extract", END)
    return graph.compile(checkpointer=checkpointer)


async def run_turn(
    graph: Any,
    *,
    thread_id: str,
    message: str,
    provider: Any,
    tools: dict[str, Any],
    sink: Any,
    agent_id: str = "default",
) -> dict[str, Any]:
    """Run one conversational turn; provider events land on ``sink``."""
    initial: TurnState = {
        "thread_id": thread_id,
        "message": message,
        "system": "",
        "history": [],
        "tool_texts": [],
        "citations": [],
        "assistant_text": "",
        "done_text": "",
    }
    config = {
        "configurable": {
            "thread_id": thread_id or "default",
            "provider": provider,
            "tools": tools,
            "sink": sink,
            "agent_id": agent_id or "default",
        }
    }
    return await graph.ainvoke(initial, config)


__all__ = ["TurnState", "build_graph", "run_turn"]
