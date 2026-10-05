"""Agent graph tests (V8): orchestration over the hardened provider.

The provider loop is untouched (see test_providers.py); these tests
prove the graph layer: events forwarded to the sink in order, turns
persisted per thread, history reloaded on the next turn, checkpointing
works, and provider faults arrive as error events (never raised).
"""

from __future__ import annotations

import asyncio

from langgraph.checkpoint.memory import MemorySaver


def run(coro):
    return asyncio.run(coro)


def make_provider(**kwargs):
    from app.llm.provider import OpenAICompatibleProvider

    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("model", "test-model")
    return OpenAICompatibleProvider(**kwargs)


def scripted(chunks):
    async def _gen(payload):
        for chunk in chunks:
            yield chunk

    return _gen


def drain(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_turn_forwards_tokens_and_persists(tmp_path, monkeypatch):
    from app.agent.graph import build_graph, run_turn

    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "g1.db"))
    provider = make_provider(
        post_stream_fn=scripted([{"content": "hello "}, {"content": "there"}])
    )
    queue: asyncio.Queue = asyncio.Queue()
    graph = build_graph()
    out = run(
        run_turn(
            graph,
            thread_id="t1",
            message="hi",
            provider=provider,
            tools={},
            sink=queue,
        )
    )
    events = drain(queue)
    kinds = [e["type"] for e in events]
    assert kinds == ["token", "token", "done"]
    assert "".join(
        e.get("text", "") for e in events if e["type"] == "token"
    ) == "hello there"
    assert out["done_text"] == "hello there"

    from app.agent.context import list_threads, load_turns

    threads = list_threads()
    assert [t["thread_id"] for t in threads] == ["t1"]
    assert threads[0]["title"] == "hi"
    turns = load_turns("t1")
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "hi"),
        ("assistant", "hello there"),
    ]


def test_second_turn_sees_first_turn_history(tmp_path, monkeypatch):
    from app.agent.graph import build_graph, run_turn

    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "g2.db"))
    seen: dict = {}

    async def source(payload):
        seen["messages"] = [dict(m) for m in payload["messages"]]
        yield {"content": "second answer"}

    provider = make_provider(post_stream_fn=source)
    graph = build_graph()

    async def go():
        queue: asyncio.Queue = asyncio.Queue()
        await run_turn(
            graph,
            thread_id="t2",
            message="first question",
            provider=make_provider(
                post_stream_fn=scripted([{"content": "first answer"}])
            ),
            tools={},
            sink=queue,
        )
        queue2: asyncio.Queue = asyncio.Queue()
        await run_turn(
            graph,
            thread_id="t2",
            message="follow up",
            provider=provider,
            tools={},
            sink=queue2,
        )

    run(go())
    roles_contents = [
        (m["role"], m["content"]) for m in seen["messages"]
    ]
    assert ("user", "first question") in roles_contents
    assert ("assistant", "first answer") in roles_contents
    assert roles_contents[-1] == ("user", "follow up")


def test_checkpointing_resumes_thread_state(tmp_path, monkeypatch):
    from app.agent.graph import build_graph, run_turn

    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "g3.db"))
    saver = MemorySaver()
    graph = build_graph(saver)
    provider = make_provider(
        post_stream_fn=scripted([{"content": "ok"}])
    )

    async def go():
        queue: asyncio.Queue = asyncio.Queue()
        return await run_turn(
            graph,
            thread_id="t3",
            message="hey",
            provider=provider,
            tools={},
            sink=queue,
        )

    out = run(go())
    assert out["done_text"] == "ok"
    state = run(graph.aget_state({"configurable": {"thread_id": "t3"}}))
    assert state.values.get("done_text") == "ok"


def test_provider_fault_becomes_error_event(tmp_path, monkeypatch):
    from app.agent.graph import build_graph, run_turn

    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "g4.db"))

    async def boom(payload):
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover - never reached

    provider = make_provider(post_stream_fn=boom)
    queue: asyncio.Queue = asyncio.Queue()
    graph = build_graph()
    out = run(
        run_turn(
            graph,
            thread_id="t4",
            message="hi",
            provider=provider,
            tools={},
            sink=queue,
        )
    )
    events = drain(queue)
    assert len(events) == 1 and events[0]["type"] == "error"
    assert events[0]["code"] == "STREAM_FAILED"
    assert out["done_text"] == ""
