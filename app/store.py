"""SQLite open + migrate helper (V1 minimal).

Choice: an ordered ``MIGRATIONS`` list with a ``schema_version`` table
instead of alembic. V1 has a single laptop-local database with a handful
of tables; a tiny in-code runner is auditable and dependency-free.
Revisit alembic only if v1 outgrows it (branches, downgrades, or a
second database such as the relay's Postgres).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Each entry is the full DDL for one schema version. Index + 1 == version.
# Keep entries append-only: never edit an applied migration, add a new one.
MIGRATIONS: list[str] = [
    """CREATE TABLE IF NOT EXISTS identity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    agent_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    encrypted_private_key TEXT NOT NULL,
    created_at TEXT NOT NULL
)""",
]


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite file with sane V1 defaults (no migration side effect)."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
    return int(row["v"])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order; return the resulting version."""
    current = get_version(conn)
    for index in range(current, len(MIGRATIONS)):
        version = index + 1
        with conn:
            conn.executescript(MIGRATIONS[index])
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (version,),
            )
    return get_version(conn)


__all__ = ["MIGRATIONS", "get_version", "migrate", "open_db"]
