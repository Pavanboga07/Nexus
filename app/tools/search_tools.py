"""Native web tools, MCP-shaped (V5).

Each tool exposes ``name`` / ``description`` / ``input_schema`` (JSON
Schema) plus ``async execute(args) -> dict`` returning JSON-able data.
``web_search`` discovers current/external information; ``web_fetch``
reads a full page. Both are ``public-web`` data, executed for the
owner's own agent with purpose ``web-research`` (bound by the chat
loop, recorded on every tool event). Provider/fetch fn are
constructor-injected for tests; defaults are keyless DDG + guarded
fetch. Failures return a clean ``{"error": ...}`` dict — never raise
into the chat path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.search import SearchError, SearchProvider, get_provider
from app.search.fetch import FetchedPage, fetch_url

PURPOSE = "web-research"


class WebSearchTool:
    name = "web_search"
    description = (
        "Discover current or external information to answer a question; "
        "use before answering from memory. Returns titles, URLs, snippets."
    )
    data_category = "public-web"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, provider: SearchProvider | None = None) -> None:
        self._provider = provider if provider is not None else get_provider()

    async def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query", "")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("web_search requires a non-empty 'query' string.")
        count = args.get("count", 5)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 5
        count = max(1, min(10, count))
        try:
            results = await self._provider.search(query.strip(), count)
        except SearchError as exc:
            return {"error": str(exc) or "Web search failed."}
        return {
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results
            ]
        }


class WebFetchTool:
    name = "web_fetch"
    description = (
        "Read the full content of a URL from search results. "
        "Call only with URLs returned by web_search or supplied by the user."
    )
    data_category = "public-web"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        fetch_fn: Callable[..., Awaitable[FetchedPage]] | None = None,
    ) -> None:
        self._fetch = fetch_fn if fetch_fn is not None else fetch_url

    async def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        url = args.get("url", "")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("web_fetch requires a non-empty 'url' string.")
        try:
            page = await self._fetch(url.strip())
        except SearchError as exc:
            return {"error": str(exc) or "Web fetch failed."}
        return {"url": page.url, "title": page.title, "text": page.text}


__all__ = ["PURPOSE", "WebFetchTool", "WebSearchTool"]
