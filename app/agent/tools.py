"""Provider + tool construction for the conversational agent.

Moved out of the chat route so the LangGraph nodes and the route share
one construction path. The route re-exports ``build_provider`` and
``build_tools`` under its own module names (existing tests monkeypatch
``app.api.routes.chat.build_provider``).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.llm.provider import OpenAICompatibleProvider, get_llm_config
from app.tools.search_tools import WebFetchTool, WebSearchTool

Executor = Callable[[str, dict[str, Any]], Awaitable[str]]


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


async def execute_tool(
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


def make_executor(tools: dict[str, Any]) -> Executor:
    """Build the provider executor closure over these tools.

    Every chat-turn tool call runs purpose-bound as web-research;
    public-web reads by the owner's own agent need no peer approval.
    """

    async def executor(name: str, args: dict[str, Any]) -> str:
        return await execute_tool(tools, name, args)

    return executor


__all__ = [
    "Executor",
    "build_provider",
    "build_tools",
    "execute_tool",
    "make_executor",
    "tool_schemas",
]
