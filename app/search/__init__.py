"""Keyless web search seam (V5).

Re-export surface only (finding F9) — the implementation lives in
:mod:`app.search.providers` so this package ``__init__`` stays thin.
``SearchProvider`` is the seam chat tools call.
"""

from __future__ import annotations

from app.search.providers import (
    DuckDuckGoProvider,
    SearchError,
    SearchProvider,
    SearchResult,
    SearchTimeoutError,
    TavilyProvider,
    get_provider,
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
