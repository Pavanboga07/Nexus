"""Shared FastAPI dependencies for the route modules (finding A3).

Five route modules defined byte-identical ``get_conn()`` dependency
bodies. This is the single copy; route modules should use it directly::

    from app.api.deps import get_conn

    @router.get("/x")
    def read_x(conn: sqlite3.Connection = Depends(get_conn)):
        ...

The ``makedirs`` guard lives centrally in :func:`app.store.open_db`, so
this helper only opens, migrates, and closes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from app.store import db_path, migrate, open_db


def get_conn() -> Iterator[sqlite3.Connection]:
    """Per-request SQLite connection (migrated, closed after)."""
    conn = open_db(db_path())
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()
