"""Best-effort domain-event emission without import cycles (finding A4).

``emit_event`` lives in :mod:`app.autonomy` (the event/trigger engine), but
autonomy sits inside the lazy-import cycle cluster
(tasks <-> workflows <-> autonomy <-> orchestration <-> execution), so
importing it at module top-level from the other cluster members would
close a real import cycle. The historical workaround was a copy-pasted
lazy-import-and-swallow block in every caller — which also silently
dropped observability events on failure.

This module is the single, documented home for that pattern: the import
stays function-level (the cycle is REAL — see the note on
:func:`emit_best_effort`) but a failure is logged at debug instead of
being swallowed by a bare ``pass``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


def emit_best_effort(conn: sqlite3.Connection, event_type: str,
                      payload: dict[str, Any] | None = None,
                      **kwargs: Any) -> None:
    """Call ``app.autonomy.emit_event``; never raises.

    WHY the import stays lazy: ``app.autonomy`` is part of the core cycle
    cluster. For example ``app.tasks`` cannot import ``app.autonomy`` at
    module top-level because ``app.autonomy`` imports ``app.workflows``
    (top-level), which imports ``app.tasks`` (top-level) — hoisting here
    would turn the lazy cycle into an ``ImportError``. The lazy import is
    the documented dodge, not an accident; the dodge used to also swallow
    the failure silently, which this helper fixes by logging at debug.
    """
    try:
        from app import autonomy as autonomy_mod

        autonomy_mod.emit_event(conn, event_type, payload, **kwargs)
    except Exception:
        logger.debug("best-effort event %r dropped", event_type,
                     exc_info=True)
