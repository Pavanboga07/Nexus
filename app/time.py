"""Single canonical clock for the app (finding A5).

Several modules defined their own ``_utcnow``/``_iso`` helpers with
*different* output formats (fractional ``datetime.isoformat()`` vs
second-resolution ``...Z``). The canonical form is second-resolution UTC
with a ``Z`` suffix — the same shape as the A2A envelope protocol's
``relay.envelope.TIMESTAMP_FORMAT`` — so timestamps also compare correctly
as plain strings (e.g. ``expires_at <= now`` in :mod:`app.tasks`) and parse
with ``datetime.fromisoformat`` and ``strptime(TIMESTAMP_FORMAT)`` alike.
"""

from __future__ import annotations

from datetime import datetime, timezone

#: Mirrors ``relay.envelope.TIMESTAMP_FORMAT``. Kept as a literal (rather
#: than importing it) so this module stays a dependency-free leaf with
#: zero inward edges — importable from anywhere, including the cycle
#: cluster, without side effects.
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    """Current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    """Second-resolution ISO-8601 UTC string (``2026-10-09T14:07:51Z``)."""
    dt = moment if moment is not None else utcnow()
    return dt.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)
