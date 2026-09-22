"""Keyless web search seam (V5).

``SearchProvider`` is the seam chat tools call; v1 ships the keyless
DuckDuckGo HTML provider only. Selectors (``result__a`` /
``result__snippet``) match DDG's ``/html/`` endpoint. Stdlib
``html.parser`` only — no new dependencies.
"""

from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from html.parser import HTMLParser
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

    def handle_starttag(self, tag, attrs) -> None:
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
    """Search via DuckDuckGo's keyless HTML endpoint."""

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


def get_provider(
    name: str = "duckduckgo", tavily_key: str | None = None
) -> SearchProvider:
    """Return the named provider (default: keyless DuckDuckGo)."""
    if name == "duckduckgo":
        return DuckDuckGoProvider()
    if name == "tavily":
        if not tavily_key:
            raise ValueError(
                "Tavily search provider requires an API key: pass "
                "tavily_key=... or set NEXUS_TAVILY_API_KEY."
            )
        raise NotImplementedError("Tavily search provider is not implemented yet.")
    raise ValueError(
        f"Unknown search provider: {name!r} (expected 'duckduckgo' or 'tavily')."
    )


__all__ = [
    "DuckDuckGoProvider",
    "SearchError",
    "SearchProvider",
    "SearchResult",
    "SearchTimeoutError",
    "get_provider",
]
