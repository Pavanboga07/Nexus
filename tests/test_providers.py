"""V7 provider-quirk shape-contract suite (TDD red first).

NO-LIVE-KEY DEVIATION (explicit, per task brief): the V7 plan calls for
ONE live session against the configured provider to record fixtures, but
this repo has no configured key (``NEXUS_LLM_API_KEY`` is unset, reading
the old project's ``.env`` is forbidden, and no test key exists), so no
live call can be made here. Fixtures below are therefore HAND-BUILT from
the KNOWN production quirk behaviors named in the task brief — nameless
tool calls, missing ids, ``extra_content``/thought-signature blobs,
``tool_choice`` behaviors, digest fallback — plus the plan-mandated
shapes (round trip, multi-call batch, refusal, timeout, rate-limit).

They are shape-contract tests: wire-shaped dicts go through OUR parsing
code and each test asserts OUR handling (drop / synthesize / preserve /
fallback). Zero live calls in this suite. The FIRST live run happens in
V8's two-laptop gate with an operator-owned key; if live traffic shows a
new shape, it lands here as a new fixture + regression test.
"""

from __future__ import annotations

# SECTION: helpers + fixtures
import asyncio
import copy
import json

import httpx
import pytest

from app.llm.provider import (
    OpenAICompatibleProvider,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

DEAD_END_TEXT = "couldn't put together an answer"


def run(coro):
    return asyncio.run(coro)


def make_provider(**kwargs):
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("model", "test-model")
    return OpenAICompatibleProvider(**kwargs)


def scripted_stream(chunks):
    async def _gen(payload):
        for chunk in chunks:
            yield chunk

    return _gen


def wire_stream(events):
    """Replay raw provider SSE events through OUR normalization."""

    async def _gen(payload):
        for event in events:
            yield OpenAICompatibleProvider._chunk_from_event(event)

    return _gen


def search_json(title, url, snippet):
    return json.dumps(
        {"results": [{"title": title, "url": url, "snippet": snippet}]}
    )


async def _null_executor(name, args):
    raise AssertionError("no tools expected")


# -- hand-built wire fixtures (OpenAI-compatible SSE event shapes) --
WIRE_TOOL_EVENT = {
    "id": "chatcmpl-roundtrip",
    "choices": [
        {
            "index": 0,
            "delta": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_alpha",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "alpha"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
}

WIRE_NAMELESS_EVENT = {
    "id": "chatcmpl-nameless",
    "choices": [
        {
            "index": 0,
            "delta": {
                "role": "assistant",
                "content": "hi",
                # Production incident: some gateways emit a tool_calls
                # entry with an empty name (streaming fragment with only
                # an index). It must be dropped, never executed.
                "tool_calls": [
                    {"index": 0, "id": "call_ghost", "function": {}}
                ],
            },
            "finish_reason": "stop",
        }
    ],
}

WIRE_MISSING_ID_EVENT = {
    "id": "chatcmpl-noid",
    "choices": [
        {
            "index": 0,
            "delta": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        # Production incident: no "id" key at all.
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "beta"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
}

WIRE_BATCH_EVENT = {
    "id": "chatcmpl-batch",
    "choices": [
        {
            "index": 0,
            "delta": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_one",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "alpha"}',
                        },
                    },
                    {
                        "index": 1,
                        "id": "call_two",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "beta"}',
                        },
                    },
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
}

WIRE_REFUSAL_EVENT = {
    "id": "chatcmpl-refusal",
    "choices": [
        {
            "index": 0,
            "delta": {
                "role": "assistant",
                "refusal": "I can't help with that request.",
            },
            "finish_reason": "stop",
        }
    ],
}

THOUGHT_BLOB = {"google": {"thought_signature": "sig-abc-123"}}
CALL_BLOB = {"anthropic": {"signature": "sig-xyz-789"}}

# SECTION: core shapes
def test_tool_call_round_trip_shape():
    """Normal OpenAI-shape tool call: parsed, executed, answer cites."""
    calls = {"n": 0}

    async def source(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            async for chunk in wire_stream([WIRE_TOOL_EVENT])(payload):
                yield chunk
        else:
            yield {"content": "Alpha is described [1](https://example.com/alpha)."}

    async def executor(name, args):
        assert name == "web_search"
        assert args == {"query": "alpha"}
        return search_json("Alpha", "https://example.com/alpha", "alpha snippet")

    provider = make_provider(post_stream_fn=source)

    async def collect():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "what is alpha?"}],
                [{"name": "web_search"}],
                executor,
            )
        ]

    events = run(collect())
    kinds = [e["type"] for e in events]
    assert kinds[0] == "tool_pending"
    assert "tool_completed" in kinds
    done = events[-1]
    assert done["type"] == "done"
    assert "https://example.com/alpha" in done["text"]
    assert "https://example.com/alpha" in done["citations"]


def test_nameless_tool_call_dropped():
    """Empty-name call is dropped: no tool events, content still flows."""

    async def executor(name, args):
        raise AssertionError(f"nameless call must not execute: {name!r}")

    provider = make_provider(post_stream_fn=wire_stream([WIRE_NAMELESS_EVENT]))

    async def collect():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "hi"}],
                [{"name": "web_search"}],
                executor,
            )
        ]

    events = run(collect())
    assert [e["type"] for e in events] == ["token", "done"]
    assert events[0]["text"] == "hi"
    assert events[-1]["text"] == "hi"


def test_missing_id_synthesized():
    """Call without an id still executes; history carries a synth id."""
    payloads: list[dict] = []

    async def source(payload):
        payloads.append(copy.deepcopy(payload))
        async for chunk in wire_stream([WIRE_MISSING_ID_EVENT])(payload):
            yield chunk

    async def executor(name, args):
        assert name == "web_search"
        return search_json("Beta", "https://example.com/beta", "beta snippet")

    provider = make_provider(post_stream_fn=source)

    async def collect():
        events = [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "beta?"}],
                [{"name": "web_search"}],
                executor,
            )
        ]
        return events

    events = run(collect())
    assert any(e["type"] == "tool_pending" for e in events)
    round_two = payloads[1]
    assistant_msgs = [
        m
        for m in round_two["messages"]
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(assistant_msgs) == 1
    synth_id = assistant_msgs[0]["tool_calls"][0]["id"]
    assert isinstance(synth_id, str) and synth_id.startswith("call_")
    tool_msgs = [
        m for m in round_two["messages"] if m.get("role") == "tool"
    ]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == synth_id


def test_multi_call_batch():
    """Two calls in one round both execute; digest cites both."""
    calls = {"n": 0}

    async def source(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            async for chunk in wire_stream([WIRE_BATCH_EVENT])(payload):
                yield chunk
        else:
            yield {"content": ""}

    async def executor(name, args):
        query = args["query"]
        return search_json(
            query.title(), f"https://example.com/{query}", f"{query} snippet"
        )

    provider = make_provider(post_stream_fn=source)

    async def collect():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "alpha and beta?"}],
                [{"name": "web_search"}],
                executor,
            )
        ]

    events = run(collect())
    pendings = [e for e in events if e["type"] == "tool_pending"]
    assert len(pendings) == 2
    assert [e for e in events if e["type"] == "tool_completed"].__len__() == 2
    done = events[-1]
    assert done["type"] == "done"
    assert "https://example.com/alpha" in done["text"]
    assert "https://example.com/beta" in done["text"]
    assert DEAD_END_TEXT not in done["text"]


def test_refusal_shape_surfaced_as_text():
    """A provider refusal is surfaced as answer text, never swallowed."""
    chunk = OpenAICompatibleProvider._chunk_from_event(WIRE_REFUSAL_EVENT)
    assert chunk.get("content") == "I can't help with that request."

    provider = make_provider(post_stream_fn=wire_stream([WIRE_REFUSAL_EVENT]))

    async def collect():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "do the bad thing"}],
                [{"name": "web_search"}],
                _null_executor,
            )
        ]

    events = run(collect())
    done = events[-1]
    assert done["type"] == "done"
    assert "I can't help with that request." in done["text"]

# SECTION: faults
def test_timeout_shape_maps_to_typed_error():
    """A transport timeout surfaces as ProviderTimeoutError, not raw httpx."""

    async def source(payload):
        raise httpx.ConnectTimeout("slow")
        yield {"content": "never"}  # pragma: no cover - makes this a generator

    provider = make_provider(post_stream_fn=source)

    async def collect():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "hi"}],
                [{"name": "web_search"}],
                _null_executor,
            )
        ]

    with pytest.raises(ProviderTimeoutError) as exc_info:
        run(collect())
    assert exc_info.value.code == "PROVIDER_TIMEOUT"


def _rate_limit_error() -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST", "https://api.example.test/v1/chat/completions"
    )
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


def test_rate_limit_shape_maps_to_typed_error():
    """An HTTP 429 surfaces as ProviderRateLimitError, not raw httpx."""

    async def source(payload):
        raise _rate_limit_error()
        yield {"content": "never"}  # pragma: no cover - makes this a generator

    provider = make_provider(post_stream_fn=source)

    async def collect():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "hi"}],
                [{"name": "web_search"}],
                _null_executor,
            )
        ]

    with pytest.raises(ProviderRateLimitError) as exc_info:
        run(collect())
    assert exc_info.value.code == "PROVIDER_RATE_LIMITED"


def test_digest_fallback_regression():
    """Always-tool-calling provider ends in a cited digest (V5 shape)."""

    async def always_tools(payload):
        yield {
            "tool_calls": [
                {
                    "id": "call_x",
                    "name": "web_search",
                    "arguments": {"query": "q"},
                }
            ]
        }

    async def executor(name, args):
        return search_json("Alpha", "https://example.com/alpha", "s")

    provider = make_provider(post_stream_fn=always_tools)

    async def collect():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "q"}],
                [{"name": "web_search"}],
                executor,
            )
        ]

    events = run(collect())
    done = events[-1]
    assert done["type"] == "done"
    assert "https://example.com/alpha" in done["text"]
    assert "https://example.com/alpha" in done["citations"]
    assert DEAD_END_TEXT not in done["text"]

# SECTION: passthrough + payload
def test_thought_signature_passthrough():
    """Opaque thought-signature blobs round-trip into history verbatim.

    Production incident: Gemini-class providers return
    ``extra_content`` blobs (e.g. ``google.thought_signature``) that MUST
    be echoed back on the next request or the follow-up call fails. Our
    code never interprets them — it preserves them at both the message
    level and the per-tool-call level.
    """
    payloads: list[dict] = []
    calls = {"n": 0}

    async def source(payload):
        payloads.append(copy.deepcopy(payload))
        calls["n"] += 1
        if calls["n"] == 1:
            yield {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_sig",
                        "name": "web_search",
                        "arguments": '{"query": "alpha"}',
                        "extra_content": dict(CALL_BLOB),
                    }
                ],
                "extra_content": dict(THOUGHT_BLOB),
            }
        else:
            yield {"content": "done"}

    async def executor(name, args):
        return search_json("Alpha", "https://example.com/alpha", "s")

    provider = make_provider(post_stream_fn=source)

    async def collect():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "alpha?"}],
                [{"name": "web_search"}],
                executor,
            )
        ]

    events = run(collect())
    assert events[-1]["type"] == "done"
    round_two = payloads[1]
    assistant_msgs = [
        m
        for m in round_two["messages"]
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0].get("extra_content") == THOUGHT_BLOB
    assert assistant_msgs[0]["tool_calls"][0].get("extra_content") == CALL_BLOB


def test_tool_choice_payload_behavior():
    """tools/tool_choice ride only when schemas exist (gateway quirk).

    Production incident: some OpenAI-compatible gateways reject an empty
    ``tools: []`` array or a stray ``tool_choice`` with no tools. Payload
    carries both (``tool_choice: "auto"``) exactly when tool schemas are
    present, and neither otherwise.
    """
    payloads: list[dict] = []

    async def source(payload):
        payloads.append(copy.deepcopy(payload))
        yield {"content": "hi"}

    provider = make_provider(post_stream_fn=source)

    async def with_tools():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "hi"}],
                [{"name": "web_search"}],
                _null_executor,
            )
        ]

    async def without_tools():
        return [
            event
            async for event in provider.stream_with_tools(
                [{"role": "user", "content": "hi"}], [], _null_executor
            )
        ]

    run(with_tools())
    assert payloads[0].get("tools") == [{"name": "web_search"}]
    assert payloads[0].get("tool_choice") == "auto"

    payloads.clear()
    run(without_tools())
    assert "tools" not in payloads[0]
    assert "tool_choice" not in payloads[0]


def test_sse_error_mapping_for_provider_faults(monkeypatch):
    """Typed provider faults reach SSE as coded error events."""
    import app.api.routes.chat as chat_route

    monkeypatch.setattr(chat_route, "_extract_after_turn", lambda *a: None)

    async def timeout_source(payload):
        raise httpx.ConnectTimeout("slow")
        yield {"content": "never"}  # pragma: no cover - generator marker

    async def limited_source(payload):
        raise _rate_limit_error()
        yield {"content": "never"}  # pragma: no cover - generator marker

    async def collect(provider):
        return [e async for e in chat_route._events("hi", provider, {})]

    events = run(collect(make_provider(post_stream_fn=timeout_source)))
    assert '"type": "error"' in events[-1]
    assert "PROVIDER_TIMEOUT" in events[-1]

    events = run(collect(make_provider(post_stream_fn=limited_source)))
    assert '"type": "error"' in events[-1]
    assert "PROVIDER_RATE_LIMITED" in events[-1]

