"""Web search provider implementations (finding F9: moved out of ``__init__``).

``SearchProvider`` is the seam chat tools call; v1 ships the keyless
DuckDuckGo HTML provider plus an optional Tavily API provider.
"""

from __future__ import annotations

import html
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

_WHITESPACE = re.compile(r"\s+")


@dataclass
class SearchResult:
    """One normalized search hit."""

    title: str
    url: str
    snippet: str


class SearchError(Exception):
    """Base error for search provider failures."""


class SearchTimeoutError(SearchError):
    """The provider did not respond within its timeout (after a retry)."""


class SearchProvider(ABC):
    """Async web search returning normalized results."""

    @abstractmethod
    async def search(self, query: str, count: int = 5) -> list[SearchResult]:
        """Search the web; at most ``count`` results."""
        raise NotImplementedError


def _real_url(href: str) -> str:
    """Unwrap DDG's ``//duckduckgo.com/l/?uddg=<target>`` redirect links."""
    target = html.unescape(href)
    if target.startswith("//"):
        target = "https:" + target
    uddg = parse_qs(urlparse(target).query).get("uddg")
    if uddg and uddg[0]:
        return uddg[0]
    return target


def _clean(text: str) -> str:
    return _WHITESPACE.sub(" ", html.unescape(text)).strip()


class _DDGParser(HTMLParser):
    """Collect (title, href, snippet) triples in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[str, str, str]] = []
        self._pending: tuple[str, str] | None = None
        self._capture: str | None = None
        self._buf: list[str] = []
        self._href: str = ""

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        classes = (dict(attrs).get("class") or "").split()
        if "result__a" in classes:
            self._flush_pending()
            self._capture = "title"
            self._buf = []
            self._href = dict(attrs).get("href") or ""
        elif "result__snippet" in classes:
            self._capture = "snippet"
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._capture is None:
            return
        text = _clean("".join(self._buf))
        if self._capture == "title":
            self._pending = (text, self._href)
        else:
            title, href = self._pending if self._pending is not None else ("", "")
            self._pending = None
            self.results.append((title, href, text))
        self._capture = None
        self._buf = []

    def close(self) -> None:
        self._flush_pending()
        super().close()

    def _flush_pending(self) -> None:
        if self._pending is not None:
            title, href = self._pending
            self.results.append((title, href, ""))
            self._pending = None


class DuckDuckGoProvider(SearchProvider):
    """Search via DuckDuckGo's keyless HTML endpoint.

    Selectors (``result__a`` / ``result__snippet``) match DDG's ``/html/``
    endpoint. Stdlib ``html.parser`` only — no new dependencies.
    """

    def __init__(
        self,
        base_url: str = "https://html.duckduckgo.com",
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    async def search(self, query: str, count: int = 5) -> list[SearchResult]:
        url = f"{self._base_url}/html/?q={quote_plus(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        last_timeout: httpx.TimeoutException | None = None
        for _ in range(2):  # initial attempt + one retry
            try:
                if self._client is not None:
                    response = await self._client.get(url, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.get(url, headers=headers)
                response.raise_for_status()
                return self._parse(response.text, count)
            except httpx.TimeoutException as exc:
                last_timeout = exc
            except httpx.HTTPError as exc:
                raise SearchError(f"DuckDuckGo search failed: {exc}") from exc
        raise SearchTimeoutError(
            f"DuckDuckGo search timed out after {self._timeout}s "
            f"(query={query!r})."
        ) from last_timeout

    @staticmethod
    def _parse(page: str, count: int) -> list[SearchResult]:
        parser = _DDGParser()
        parser.feed(page)
        parser.close()
        return [
            SearchResult(title=title, url=_real_url(href), snippet=snippet)
            for title, href, snippet in parser.results
            if title or href
        ][:count]


class TavilyProvider(SearchProvider):
    """Search via Tavily's API (requires an API key).

    Finding F5: ``get_provider("tavily", tavily_key=...)`` used to raise a
    bare ``NotImplementedError`` even with a valid key, making
    ``NEXUS_TAVILY_API_KEY`` a dead env var. This is the real
    implementation against ``POST {base_url}/search``.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not (api_key or "").strip():
            raise ValueError("TavilyProvider requires a non-empty API key.")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    async def search(self, query: str, count: int = 5) -> list[SearchResult]:
        url = f"{self._base_url}/search"
        payload = {
            "api_key": self._api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": max(1, count),
            "include_answer": False,
        }
        last_timeout: httpx.TimeoutException | None = None
        for _ in range(2):  # initial attempt + one retry
            try:
                if self._client is not None:
                    response = await self._client.post(url, json=payload)
                else:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.post(url, json=payload)
                if response.status_code == 401:
                    raise SearchError(
                        "Tavily rejected the API key (HTTP 401): check "
                        "NEXUS_TAVILY_API_KEY."
                    )
                response.raise_for_status()
                return self._parse(response.json(), count)
            except httpx.TimeoutException as exc:
                last_timeout = exc
            except httpx.HTTPError as exc:
                raise SearchError(f"Tavily search failed: {exc}") from exc
        raise SearchTimeoutError(
            f"Tavily search timed out after {self._timeout}s "
            f"(query={query!r})."
        ) from last_timeout

    @staticmethod
    def _parse(body: Any, count: int) -> list[SearchResult]:
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise SearchError(
                f"Tavily returned an unexpected body: {str(body)[:200]}"
            )
        out = []
        for item in results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "")
            link = str(item.get("url") or "")
            snippet = _clean(str(item.get("content") or ""))
            if title or link:
                out.append(SearchResult(title=title, url=link,
                                       snippet=snippet))
        return out[:count]


def get_provider(
    name: str = "duckduckgo", tavily_key: str | None = None
) -> SearchProvider:
    """Return the named provider (default: keyless DuckDuckGo)."""
    if name == "duckduckgo":
        return DuckDuckGoProvider()
    if name == "tavily":
        key = (tavily_key or "").strip() or os.environ.get(
            "NEXUS_TAVILY_API_KEY", "").strip()
        if not key:
            raise ValueError(
                "Tavily search provider requires an API key: pass "
                "tavily_key=... or set NEXUS_TAVILY_API_KEY."
            )
        return TavilyProvider(key)
    raise ValueError(
        f"Unknown search provider: {name!r} (expected 'duckduckgo' or 'tavily')."
    )


__all__ = [
    "DuckDuckGoProvider",
    "SearchError",
    "SearchProvider",
    "SearchResult",
    "SearchTimeoutError",
    "TavilyProvider",
    "get_provider",
]
