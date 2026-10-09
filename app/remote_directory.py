"""Gateway directory client: discovery metadata, never trust.

Reads the remote directory (``/directory``, ``/directory/{id}``) and
runs every card through the ONE canonical verification path
(``relay.directory.verify_card``). Anything failing verification is
rejected with its reason — never trusted, never silently skipped.

Verified cards are DISCOVERY metadata only (DISCOVERED, not TRUSTED,
not AUTHORIZED). Dispatch still requires a paired + TRUSTED peer;
trust comes from the local pairing ceremony, never from the gateway.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from app.errors import NexusError

_AGENT_RE = re.compile(r"^nexus:ed25519:[0-9a-f]{32}$")
_TIMEOUT = 10.0


class RemoteDirectoryError(NexusError):
    """Directory lookup failure with machine ``code`` + HTTP ``status``.

    Renamed from ``DirectoryError`` (finding A7): ``relay/directory.py``
    defines its own ``DirectoryError`` with a different base class
    (``ValueError`` vs this module's old ``RuntimeError``), and the two
    names collided at call sites that import both.
    """


#: Deprecated alias — use :class:`RemoteDirectoryError`. Kept so existing
#: ``except remote_directory.DirectoryError`` call sites keep working.
DirectoryError = RemoteDirectoryError


def gateway_http_base() -> str:
    """Relay WS(S) base converted to its HTTPS(S) directory root."""
    import os

    raw = os.environ.get("NEXUS_RELAY_URL", "").strip()
    if raw.startswith("wss://"):
        base = "https://" + raw[len("wss://"):]
    elif raw.startswith("ws://"):
        base = "http://" + raw[len("ws://"):]
    else:
        base = raw
    try:
        parts = urlsplit(base)
    except ValueError as exc:
        raise RemoteDirectoryError(
            "BAD_GATEWAY_URL", f"relay URL is malformed: {exc}",
            status=500,
        ) from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RemoteDirectoryError(
            "BAD_GATEWAY_URL",
            "relay URL must be http(s) (or ws(s)) with a host.",
            status=500,
        )
    return base.rstrip("/")


def _check_id(agent_id: str) -> str:
    cleaned = (agent_id or "").strip()
    if not _AGENT_RE.fullmatch(cleaned):
        raise RemoteDirectoryError(
            "BAD_AGENT_ID", f"malformed agent id {cleaned!r}.",
            status=400,
        )
    return cleaned


def fetch_card(agent_id: str, *, base_url: str | None = None) -> dict:
    """Fetch + verify one directory card. Raises on any fault."""
    import httpx

    from relay.directory import DirectoryError as RelayDirectoryError
    from relay.directory import verify_card

    ident = _check_id(agent_id)
    base = (base_url or "").strip() or gateway_http_base()
    try:
        resp = httpx.get(f"{base}/directory/{ident}", timeout=_TIMEOUT)
    except Exception as exc:
        raise RemoteDirectoryError(
            "GATEWAY_UNREACHABLE", f"directory unreachable: {exc}",
            status=502,
        ) from exc
    if resp.status_code == 404:
        raise RemoteDirectoryError(
            "NOT_FOUND", f"no directory entry for {ident}.", status=404)
    if resp.status_code != 200:
        raise RemoteDirectoryError(
            "GATEWAY_ERROR",
            f"directory answered HTTP {resp.status_code}.",
            status=502,
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise RemoteDirectoryError(
            "BAD_GATEWAY_BODY", f"directory returned non-JSON: {exc}",
            status=502,
        ) from exc
    card = body.get("card") if isinstance(body, dict) else None
    if not isinstance(card, dict):
        raise RemoteDirectoryError(
            "INVALID_CARD", "directory entry has no card object.",
            status=502)
    try:
        verify_card(card)
    except RelayDirectoryError as exc:
        raise RemoteDirectoryError(
            "INVALID_CARD", f"directory card rejected: {exc}",
            status=502) from exc
    return card


def search_directory(limit: int = 20, *,
                     base_url: str | None = None) -> dict[str, Any]:
    """List directory entries; verify each card, count rejections.

    Returns ``{"entries": [verified cards], "rejected": n}``. Query
    parameters go through the HTTP client's encoder (never string
    interpolation), and pagination is bounded server- and client-side.
    """
    import httpx

    from relay.directory import DirectoryError as RelayDirectoryError
    from relay.directory import verify_card

    base = (base_url or "").strip() or gateway_http_base()
    limit = max(1, min(int(limit or 20), 50))
    try:
        resp = httpx.get(
            f"{base}/directory",
            params={"limit": limit, "offset": 0},
            timeout=_TIMEOUT,
        )
    except Exception as exc:
        raise RemoteDirectoryError(
            "GATEWAY_UNREACHABLE", f"directory unreachable: {exc}",
            status=502,
        ) from exc
    if resp.status_code != 200:
        raise RemoteDirectoryError(
            "GATEWAY_ERROR",
            f"directory answered HTTP {resp.status_code}.",
            status=502,
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise RemoteDirectoryError(
            "BAD_GATEWAY_BODY", f"directory returned non-JSON: {exc}",
            status=502,
        ) from exc
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return {"entries": [], "rejected": 0}
    entries, rejected = [], 0
    for item in items[:limit]:
        card = item.get("card") if isinstance(item, dict) else None
        if not isinstance(card, dict):
            rejected += 1
            continue
        try:
            verify_card(card)
        except RelayDirectoryError:
            rejected += 1
            continue
        entries.append(card)
    return {"entries": entries, "rejected": rejected}


__all__ = ["RemoteDirectoryError", "DirectoryError", "fetch_card",
           "gateway_http_base", "search_directory"]