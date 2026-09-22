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
    """CREATE TABLE IF NOT EXISTS invites (
    code_hash TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    card_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pair_attempts (
    key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    last_failure TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paired_peers (
     agent_id TEXT PRIMARY KEY,
     public_key TEXT NOT NULL,
     display_name TEXT NOT NULL,
     fingerprint TEXT NOT NULL,
     card_json TEXT NOT NULL,
     paired_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS a2a_messages (
     message_id TEXT PRIMARY KEY,
     correlation_id TEXT NOT NULL,
     sender TEXT NOT NULL,
     recipient TEXT NOT NULL,
     message_type TEXT NOT NULL,
     envelope_json TEXT NOT NULL,
     status TEXT NOT NULL DEFAULT 'stored',
     created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_a2a_messages_corr
    ON a2a_messages (correlation_id);
CREATE TABLE IF NOT EXISTS a2a_approvals (
     approval_id TEXT PRIMARY KEY,
     message_id TEXT NOT NULL UNIQUE,
     correlation_id TEXT NOT NULL,
     requester TEXT NOT NULL,
     action TEXT NOT NULL,
     data_category TEXT NOT NULL,
     purpose TEXT NOT NULL,
     question TEXT NOT NULL DEFAULT '',
     expires_at TEXT NOT NULL,
     status TEXT NOT NULL DEFAULT 'pending',
     decided_at TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_rules (
     rule_id TEXT PRIMARY KEY,
     peer TEXT NOT NULL DEFAULT '*',
     data_category TEXT NOT NULL DEFAULT '*',
     purpose TEXT NOT NULL DEFAULT '*',
     action TEXT NOT NULL DEFAULT '*',
     effect TEXT NOT NULL DEFAULT 'ALLOW',
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
