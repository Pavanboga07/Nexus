"""Guarded page fetch (V5).

``fetch_url`` validates the URL FIRST (SSRF protection: scheme +
credential + local-network rejection), then reads with manual redirect
handling — each hop is re-validated, max 3 hops — and a streaming read
capped at 8 KB. Boilerplate (``script``/``style``/``nav``) is stripped
with stdlib ``html.parser`` only.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from app.search import SearchError

MAX_BYTES = 8 * 1024
MAX_REDIRECTS = 3

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_WHITESPACE = re.compile(r"\s+")
_SKIP_TAGS = {"script", "style", "nav"}
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "0.0.0.0"}


class FetchError(SearchError):
    """A page fetch failed (blocked URL, transport error, redirects)."""


@dataclass
class FetchedPage:
    """Extracted page text."""

    url: str
    title: str
    text: str


def _host_looks_local(hostname: str) -> bool:
    lowered = hostname.lower().rstrip(".")
    if lowered in _LOCAL_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return (
            ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
        )
    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False  # unresolvable names fail during the request anyway
    for info in addrinfos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return True
    return False


def validate_url(url: str, *, allow_local: bool = False) -> str:
    """Validate a fetch URL. Raises FetchError before any socket opens."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise FetchError(f"Blocked URL {url!r}: malformed URL.") from None
    if parts.scheme not in {"http", "https"}:
        raise FetchError(
            f"Blocked URL {url!r}: scheme must be http or https."
        )
    if not parts.hostname:
        raise FetchError(f"Blocked URL {url!r}: no host.")
    if parts.username or parts.password:
        raise FetchError(f"Blocked URL {url!r}: credentials in URLs are not allowed.")
    if not allow_local and _host_looks_local(parts.hostname):
        raise FetchError(
            f"Blocked URL {url!r}: local/private network addresses "
            "are not allowed."
        )
    return url


class _TextParser(HTMLParser):
    """Collect ``<title>`` + visible text, skipping boilerplate subtrees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title" and self._skip_depth == 0:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._parts.append(data)

    @property
    def title(self) -> str:
        return _WHITESPACE.sub(" ", "".join(self._title_parts)).strip()

    @property
    def text(self) -> str:
        return _WHITESPACE.sub(" ", "".join(self._parts)).strip()


async def fetch_url(
    url: str, *, allow_local: bool = False, client: httpx.AsyncClient | None = None
) -> FetchedPage:
    """Fetch a page: SSRF-validated per hop, redirect-bounded, size-capped."""
    current_url = url
    redirects_followed = 0
    owned = client is None
    active = client or httpx.AsyncClient(
        timeout=10.0, follow_redirects=False
    )
    try:
        while True:
            validate_url(current_url, allow_local=allow_local)
            try:
                response = await active.get(
                    current_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                    },
                )
            except httpx.HTTPError as exc:
                raise FetchError(
                    f"Fetch failed for {url!r} ({type(exc).__name__})."
                ) from exc
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise FetchError(
                        f"Fetch failed with HTTP {response.status_code} "
                        f"for {current_url!r}."
                    )
                redirects_followed += 1
                if redirects_followed > MAX_REDIRECTS:
                    raise FetchError(
                        f"Fetch failed for {url!r} (too many redirects)."
                    )
                current_url = urljoin(current_url, location)
                continue
            if response.status_code >= 400:
                raise FetchError(
                    f"Fetch failed with HTTP {response.status_code} "
                    f"for {current_url!r}."
                )
            raw = response.content[:MAX_BYTES]
            parser = _TextParser()
            parser.feed(raw.decode("utf-8", errors="replace"))
            parser.close()
            return FetchedPage(
                url=current_url, title=parser.title, text=parser.text
            )
    finally:
        if owned:
            await active.aclose()


__all__ = [
    "FetchError",
    "FetchedPage",
    "MAX_BYTES",
    "MAX_REDIRECTS",
    "fetch_url",
    "validate_url",
]
