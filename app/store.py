"""SQLite open + migrate helper (V1 minimal).

Choice: an ordered ``MIGRATIONS`` list with a ``schema_version`` table
instead of alembic. V1 has a single laptop-local database with a handful
of tables; a tiny in-code runner is auditable and dependency-free.
Revisit alembic only if v1 outgrows it (branches, downgrades, or a
second database such as the relay's Postgres).
"""

from __future__ import annotations

import os
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
    """CREATE TABLE IF NOT EXISTS chat_threads (
     thread_id TEXT PRIMARY KEY,
     title TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_turns (
     turn_id TEXT PRIMARY KEY,
     thread_id TEXT NOT NULL REFERENCES chat_threads(thread_id)
         ON DELETE CASCADE,
     role TEXT NOT NULL,
     text TEXT NOT NULL DEFAULT '',
     citations_json TEXT NOT NULL DEFAULT '[]',
     created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_thread
    ON chat_turns (thread_id)""",
    """CREATE TABLE IF NOT EXISTS agents (
     id TEXT PRIMARY KEY,
     owner_id TEXT NOT NULL DEFAULT 'local',
     agent_id TEXT NOT NULL UNIQUE,
     display_name TEXT NOT NULL DEFAULT '',
     status TEXT NOT NULL DEFAULT 'active',
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_identities (
     key_id TEXT PRIMARY KEY,
     agent_id TEXT NOT NULL REFERENCES agents(agent_id)
         ON DELETE CASCADE,
     version INTEGER NOT NULL,
     public_key TEXT NOT NULL,
     encrypted_private_key TEXT NOT NULL,
     fingerprint TEXT NOT NULL DEFAULT '',
     status TEXT NOT NULL DEFAULT 'active',
     created_at TEXT NOT NULL,
     revoked_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_identities_agent
    ON agent_identities (agent_id, version)""",
    # NOTE (Phase 2): these three entries were appended LAST on purpose.
    # Migration versions are positional — inserting mid-list renumbers
    # every version after it, so already-migrated databases silently skip
    # the newcomer. Always append; never insert.
    """ALTER TABLE policy_rules ADD COLUMN agent_id TEXT NOT NULL
    DEFAULT '*'""",
    """CREATE TABLE IF NOT EXISTS capabilities (
     id TEXT PRIMARY KEY,
     agent_id TEXT NOT NULL REFERENCES agents(agent_id)
         ON DELETE CASCADE,
     name TEXT NOT NULL,
     version INTEGER NOT NULL,
     description TEXT NOT NULL DEFAULT '',
     input_schema_json TEXT NOT NULL DEFAULT '{}',
     output_schema_json TEXT NOT NULL DEFAULT '{}',
     tool TEXT,
     status TEXT NOT NULL DEFAULT 'active',
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capabilities_agent
    ON capabilities (agent_id)""",
    """CREATE TABLE IF NOT EXISTS delegations (
     id TEXT PRIMARY KEY,
     issuer_agent_id TEXT NOT NULL,
     recipient_agent_id TEXT NOT NULL,
     capability_id TEXT NOT NULL,
     purpose TEXT NOT NULL DEFAULT '',
     task_id TEXT NOT NULL DEFAULT '',
     constraints_json TEXT NOT NULL DEFAULT '{}',
     issued_at TEXT NOT NULL,
     expires_at TEXT NOT NULL,
     revoked_at TEXT NOT NULL DEFAULT '',
     status TEXT NOT NULL DEFAULT 'active',
     signature TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delegations_parties
    ON delegations (recipient_agent_id, status);
CREATE TABLE IF NOT EXISTS delegation_events (
     event_id TEXT PRIMARY KEY,
     delegation_id TEXT NOT NULL REFERENCES delegations(id)
         ON DELETE CASCADE,
     action TEXT NOT NULL,
     detail TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delegation_events_grant
    ON delegation_events (delegation_id)""",
    """CREATE TABLE IF NOT EXISTS tasks (
     task_id TEXT PRIMARY KEY,
     owner_id TEXT NOT NULL DEFAULT 'local',
     requesting_agent_id TEXT NOT NULL DEFAULT '',
     target_agent_id TEXT NOT NULL DEFAULT '',
     capability_id TEXT NOT NULL DEFAULT '',
     requested_target TEXT NOT NULL DEFAULT '',
     requested_capability TEXT NOT NULL DEFAULT '',
     purpose TEXT NOT NULL DEFAULT 'answer',
     status TEXT NOT NULL DEFAULT 'PENDING',
     input_json TEXT NOT NULL DEFAULT '{}',
     output_json TEXT NOT NULL DEFAULT '{}',
     error_code TEXT NOT NULL DEFAULT '',
     error_detail TEXT NOT NULL DEFAULT '',
     correlation_id TEXT NOT NULL DEFAULT '',
     parent_task_id TEXT NOT NULL DEFAULT '',
     retry_of TEXT NOT NULL DEFAULT '',
     retry_count INTEGER NOT NULL DEFAULT 0,
     delegation_id TEXT NOT NULL DEFAULT '',
     approval_id TEXT NOT NULL DEFAULT '',
     idempotency_key TEXT NOT NULL DEFAULT '',
     timeout_seconds INTEGER NOT NULL DEFAULT 300,
     deadline_at TEXT NOT NULL DEFAULT '',
     started_at TEXT NOT NULL DEFAULT '',
     completed_at TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
    ON tasks (owner_id, idempotency_key)
    WHERE idempotency_key != '';
CREATE INDEX IF NOT EXISTS idx_tasks_owner_status
    ON tasks (owner_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_correlation
    ON tasks (correlation_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent
    ON tasks (parent_task_id);
CREATE TABLE IF NOT EXISTS task_events (
     event_id TEXT PRIMARY KEY,
     task_id TEXT NOT NULL REFERENCES tasks(task_id)
         ON DELETE CASCADE,
     event TEXT NOT NULL,
     detail TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task
    ON task_events (task_id);
CREATE TABLE IF NOT EXISTS workflows (
     id TEXT PRIMARY KEY,
     owner_id TEXT NOT NULL DEFAULT 'local',
     name TEXT NOT NULL DEFAULT '',
     status TEXT NOT NULL DEFAULT 'PENDING',
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_steps (
     step_id TEXT PRIMARY KEY,
     workflow_id TEXT NOT NULL REFERENCES workflows(id)
         ON DELETE CASCADE,
     step_key TEXT NOT NULL,
     target_agent TEXT NOT NULL DEFAULT '',
     capability TEXT NOT NULL DEFAULT '',
     input_json TEXT NOT NULL DEFAULT '{}',
     delegation_id TEXT NOT NULL DEFAULT '',
     purpose TEXT NOT NULL DEFAULT 'answer',
     depends_on_json TEXT NOT NULL DEFAULT '[]',
     status TEXT NOT NULL DEFAULT 'PENDING',
     task_id TEXT NOT NULL DEFAULT '',
     result_json TEXT NOT NULL DEFAULT '{}',
     error TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_steps_flow
    ON workflow_steps (workflow_id)""",
    # NOTE: v10 uses ALTERs (existing tables). Never insert mid-list.
    """ALTER TABLE paired_peers ADD COLUMN trust_state TEXT NOT NULL
    DEFAULT 'TRUSTED';
ALTER TABLE paired_peers ADD COLUMN last_seen_at TEXT NOT NULL
    DEFAULT '';
ALTER TABLE tasks ADD COLUMN remote_task_id TEXT NOT NULL DEFAULT ''""",
    # NOTE: v11 autonomy tables + columns. Append-only, always last.
    """ALTER TABLE agents ADD COLUMN autonomy TEXT NOT NULL
    DEFAULT 'limited';
ALTER TABLE tasks ADD COLUMN origin TEXT NOT NULL DEFAULT 'user';
ALTER TABLE tasks ADD COLUMN depth INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN root_task_id TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN trigger_id TEXT NOT NULL DEFAULT '';
ALTER TABLE chat_threads ADD COLUMN agent_id TEXT NOT NULL DEFAULT 'default'""",
    """CREATE TABLE IF NOT EXISTS schedules (
     id TEXT PRIMARY KEY,
     owner_id TEXT NOT NULL DEFAULT 'local',
     agent_id TEXT NOT NULL DEFAULT '',
     name TEXT NOT NULL DEFAULT '',
     kind TEXT NOT NULL DEFAULT 'cron',
     trigger TEXT NOT NULL DEFAULT '',
     timezone TEXT NOT NULL DEFAULT 'UTC',
     target_agent TEXT NOT NULL DEFAULT '',
     capability TEXT NOT NULL DEFAULT '',
     task_input_json TEXT NOT NULL DEFAULT '{}',
     purpose TEXT NOT NULL DEFAULT 'answer',
     delegation_id TEXT NOT NULL DEFAULT '',
     timeout_seconds INTEGER NOT NULL DEFAULT 300,
     enabled INTEGER NOT NULL DEFAULT 1,
     next_run_at TEXT NOT NULL DEFAULT '',
     last_run_at TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_due
    ON schedules (enabled, next_run_at);
CREATE TABLE IF NOT EXISTS events (
     id TEXT PRIMARY KEY,
     event_type TEXT NOT NULL,
     payload_json TEXT NOT NULL DEFAULT '{}',
     agent_id TEXT NOT NULL DEFAULT '',
     depth INTEGER NOT NULL DEFAULT 0,
     root_task_id TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type
    ON events (event_type, created_at);
CREATE TABLE IF NOT EXISTS triggers (
     id TEXT PRIMARY KEY,
     owner_id TEXT NOT NULL DEFAULT 'local',
     agent_id TEXT NOT NULL DEFAULT '',
     event_type TEXT NOT NULL,
     filter_json TEXT NOT NULL DEFAULT '{}',
     target_workflow_id TEXT NOT NULL DEFAULT '',
     target_agent TEXT NOT NULL DEFAULT '',
     target_capability TEXT NOT NULL DEFAULT '',
     target_input_json TEXT NOT NULL DEFAULT '{}',
     delegation_id TEXT NOT NULL DEFAULT '',
     enabled INTEGER NOT NULL DEFAULT 1,
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
)""",
    # NOTE: v13 multi-owner. Append-only, always last.
    """CREATE TABLE IF NOT EXISTS owners (
     id TEXT PRIMARY KEY,
     display_name TEXT NOT NULL DEFAULT '',
     status TEXT NOT NULL DEFAULT 'active',
     created_at TEXT NOT NULL
);
INSERT OR IGNORE INTO owners (id, display_name, status, created_at)
    VALUES ('local', 'Local operator', 'active',
            datetime('now'));
CREATE TABLE IF NOT EXISTS operator_tokens (
     id TEXT PRIMARY KEY,
     owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
     token_hash TEXT NOT NULL UNIQUE,
     scopes TEXT NOT NULL DEFAULT 'operator',
     created_at TEXT NOT NULL,
     revoked_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_operator_tokens_hash
    ON operator_tokens (token_hash)""",
]


def db_path() -> str:
    """Canonical SQLite path: ``NEXUS_DB_PATH`` env, else ``data/nexus.db``.

    The one helper every ``os.environ.get("NEXUS_DB_PATH", "data/nexus.db")``
    call site should use (finding F8). Identical semantics to the inline
    copies it replaces.
    """
    return os.environ.get("NEXUS_DB_PATH", "data/nexus.db")


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite file with sane V1 defaults (no migration side effect)."""
    # Central makedirs guard (finding A3): every route ``get_conn()`` grew
    # its own copy of this; it lives here now so fresh checkouts never 500
    # on a missing parent dir (finding F2's class of bug).
    parent = Path(path).expanduser().parent
    if str(parent) not in ("", "."):
        os.makedirs(parent, exist_ok=True)
    # check_same_thread=False: route dependencies are generator-based
    # (``yield conn`` + close in finally), and FastAPI/anyio may run
    # setup, route body, and teardown on DIFFERENT worker threads.
    # The default True then raises ProgrammingError on the hop. SQLite's
    # own locking serializes writers; timeout covers contention.
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
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
