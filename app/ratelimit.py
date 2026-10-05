"""MVP in-memory rate limiter (no Redis, single-process).

Simple sliding-window per (key) counter. Sufficient for MVP auth
endpoints: operator-token rotation and live-ticket minting.
"""

from __future__ import annotations

import time

_buckets: dict[str, list[float]] = {}


def check_rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds).

    Prunes timestamps older than the window, then allows when fewer
    than ``limit`` hits remain in the window.
    """
    now = time.monotonic()
    hits = _buckets.get(key, [])
    fresh = [t for t in hits if t > now - window_seconds]
    if len(fresh) >= limit:
        oldest = min(fresh)
        retry = max(1, int(oldest + window_seconds - now) + 1)
        _buckets[key] = fresh
        return False, retry
    fresh.append(now)
    _buckets[key] = fresh
    return True, 0


def reset_rate_limits() -> None:
    """Test hook: clear all buckets."""
    _buckets.clear()


__all__ = ["check_rate_limit", "reset_rate_limits"]
