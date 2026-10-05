"""Agent lifecycle: CREATED→ACTIVE→DISABLED→REVOKED enforced everywhere.

Disabled agents perform no new work; revoked agents are distrusted for
new operations while history stays auditable. Re-enabling restores a
disabled agent; revocation is terminal for trust but not for records.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return db


SECRET = "lifecycle-test-secret-xxxxxxxxxxxx"


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


def test_full_lifecycle_walk(db_env):
    from app import agents

    conn = _conn(db_env)
    try:
        created = agents.create_agent(conn, SECRET, "temp")
        assert created["status"] == "active"
        agents.resolve_signing_key(conn, SECRET, "temp")
        assert agents.set_agent_status(conn, "temp", "disabled")["status"] == "disabled"
        with pytest.raises(agents.AgentError) as exc:
            agents.resolve_signing_key(conn, SECRET, "temp")
        assert exc.value.code == "NOT_ACTIVE"
        assert agents.set_agent_status(conn, "temp", "active")["status"] == "active"
        agents.resolve_signing_key(conn, SECRET, "temp")
        assert agents.set_agent_status(conn, "temp", "revoked")["status"] == "revoked"
        with pytest.raises(agents.AgentError):
            agents.resolve_signing_key(conn, SECRET, "temp")
        # History survives revocation for audit.
        assert len(agents.key_history(conn, "temp")) == 1
    finally:
        conn.close()


def test_disabled_agent_hidden_from_discovery(db_env):
    from app import agents, discovery

    conn = _conn(db_env)
    try:
        agents.create_agent(conn, SECRET, "quiet")
        assert discovery.resolve(conn, "quiet")["explicit"] is True
        agents.set_agent_status(conn, "quiet", "disabled")
        # Disabled agents stay in the registry but no longer resolve.
        assert discovery.resolve(conn, "quiet") == {
            "matches": [], "explicit": False}
    finally:
        conn.close()


def test_disabled_issuer_cannot_grant(db_env):
    from app import agents, capabilities
    from app.delegations import DelegationError, issue_grant

    conn = _conn(db_env)
    try:
        agents.create_agent(conn, SECRET, "boss")
        agents.create_agent(conn, SECRET, "worker")
        capabilities.register_capability(conn, "worker", "job")
        agents.set_agent_status(conn, "boss", "disabled")
        with pytest.raises(DelegationError) as exc:
            issue_grant(
                conn, SECRET, "boss",
                agents.get_agent(conn, "worker")["agent_id"],
                "worker.job@v1", "answer",
            )
        assert exc.value.code == "ISSUER_INACTIVE"
    finally:
        conn.close()


def test_revoked_then_rotated_recovers_signing(db_env):
    from app import agents

    conn = _conn(db_env)
    try:
        agents.create_agent(conn, SECRET, "rot")
        agents.revoke_agent_key(conn, "rot")
        with pytest.raises(agents.AgentError):
            agents.resolve_signing_key(conn, SECRET, "rot")
        rotated = agents.rotate_agent_key(conn, SECRET, "rot")
        assert rotated["key_version"] == 2
        agents.resolve_signing_key(conn, SECRET, "rot")
        versions = [k["version"] for k in agents.key_history(conn, "rot")]
        assert versions == [2, 1]
    finally:
        conn.close()


def test_disabled_capability_blocks_execution(db_env, monkeypatch):
    import asyncio
    import json

    from app import agents, capabilities
    from app.a2a import service
    from app.execution import ExecutionError, execute_capability

    async def _fake_execute(tools, name, args):
        return json.dumps({"ok": True})

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    conn = _conn(db_env)
    try:
        agents.create_agent(conn, SECRET, "doer")
        capabilities.register_capability(conn, "doer", "job", tool="web_search")
        service.create_rule(conn, peer="*")
        capabilities.set_capability_status(conn, "doer.job@v1", "disabled")
        with pytest.raises(ExecutionError) as exc:
            asyncio.run(execute_capability(
                conn, acting_ref="doer", capability_id="doer.job@v1",
                args={}, purpose="answer"))
        assert exc.value.code == "CAPABILITY_DISABLED"
    finally:
        conn.close()


def test_unknown_status_rejected(db_env):
    from app import agents

    conn = _conn(db_env)
    try:
        agents.create_agent(conn, SECRET, "x")
        with pytest.raises(agents.AgentError) as exc:
            agents.set_agent_status(conn, "x", "archived")
        assert exc.value.code == "BAD_STATUS"
    finally:
        conn.close()
