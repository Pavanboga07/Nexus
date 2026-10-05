"""Multi-owner isolation: data boundaries hold between owners."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    monkeypatch.delenv("NEXUS_OPERATOR_TOKEN", raising=False)
    return db


def _client(token):
    from app.main import app

    return TestClient(
        app, headers={"Authorization": f"Bearer {token}"})


def test_owners_crud_and_token_lifecycle(isolated_db, monkeypatch):
    from app import owners
    from app.store import migrate, open_db

    conn = open_db(isolated_db)
    migrate(conn)
    try:
        assert [o["id"] for o in owners.list_owners(conn)] == ["local"]
        beta = owners.create_owner(conn, "beta", "Beta")
        assert beta["id"] == "beta"
        with pytest.raises(owners.OwnerError):
            owners.create_owner(conn, "Bad Name!")
        issued = owners.issue_token(conn, "beta")
        assert issued["token"].startswith("nx_")
        assert issued["owner_id"] == "beta"
        resolved = owners.resolve_token(conn, issued["token"])
        assert resolved == {"owner_id": "beta",
                            "scopes": ("operator",)}
        listed = owners.list_tokens(conn, "beta")
        assert [t["id"] for t in listed] == [issued["id"]]
        assert "token_hash" not in listed[0]
        assert "token" not in listed[0]
        owners.revoke_token(conn, issued["id"])
        assert owners.resolve_token(conn, issued["token"]) is None
        with pytest.raises(owners.OwnerError):
            owners.set_owner_status(conn, "local", "disabled")
    finally:
        conn.close()


def test_cross_owner_tasks_invisible(isolated_db, monkeypatch):
    from app import agents, owners
    from app.store import migrate, open_db

    conn = open_db(isolated_db)
    migrate(conn)
    try:
        owners.create_owner(conn, "beta")
        a_token = owners.issue_token(conn, "beta")["token"]
        agents.create_agent(conn, "s", "alpha-agent", owner_id="local")
        agents.create_agent(conn, "s", "beta-agent", owner_id="beta")
        from app import tasks

        t_local = tasks.create_task(
            conn, owner_id="local", requesting_agent_id="alpha-agent",
            target_agent="alpha-agent")
        t_beta = tasks.create_task(
            conn, owner_id="beta", requesting_agent_id="beta-agent",
            target_agent="beta-agent")
        assert [t["task_id"] for t in tasks.list_tasks(
            conn, owner_id="local")] == [t_local["task_id"]]
        assert [t["task_id"] for t in tasks.list_tasks(
            conn, owner_id="beta")] == [t_beta["task_id"]]
    finally:
        conn.close()
    monkeypatch.setenv("NEXUS_OPERATOR_TOKEN", "op-local-tok")
    beta_client = _client(a_token)
    beta_tasks = beta_client.get("/tasks").json()["tasks"]
    assert [t["task_id"] for t in beta_tasks] == [t_beta["task_id"]]
    assert beta_client.get(f"/tasks/{t_local['task_id']}").status_code \
        == 404
    assert beta_client.post(
        f"/tasks/{t_local['task_id']}/cancel").status_code == 404
    local = _client("op-local-tok")
    assert [t["task_id"] for t in local.get("/tasks").json()["tasks"]] == [
        t_local["task_id"]]


def test_cross_owner_agents_invisible(isolated_db, monkeypatch):
    from app import agents, owners
    from app.store import migrate, open_db

    conn = open_db(isolated_db)
    migrate(conn)
    try:
        owners.create_owner(conn, "beta")
        a_token = owners.issue_token(conn, "beta")["token"]
        agents.create_agent(conn, "s", "mine", owner_id="local")
        agents.create_agent(conn, "s", "theirs", owner_id="beta")
    finally:
        conn.close()
    monkeypatch.setenv("NEXUS_OPERATOR_TOKEN", "op-local-tok")
    beta_client = _client(a_token)
    assert [a["id"] for a in beta_client.get(
        "/agents").json()["agents"]] == ["theirs"]
    assert beta_client.get("/agents/mine").status_code == 404
    assert beta_client.post(
        "/agents/mine/disable").status_code == 404
    local = _client("op-local-tok")
    # No identity row in this fresh DB, so no default backfill — only
    # the explicitly created agent shows, and never beta's.
    assert {a["id"] for a in local.get(
        "/agents").json()["agents"]} == {"mine"}


def test_cross_owner_memory_denied(isolated_db, monkeypatch):
    from app import agents, owners
    from app.memory.store import MemoryStore
    from app.store import migrate, open_db

    conn = open_db(isolated_db)
    migrate(conn)
    try:
        owners.create_owner(conn, "beta")
        a_token = owners.issue_token(conn, "beta")["token"]
        agents.create_agent(conn, "s", "keeper", owner_id="local")
        store = MemoryStore(isolated_db)
        try:
            store.add("local secret", agent_id="keeper")
        finally:
            store.close()
    finally:
        conn.close()
    beta_client = _client(a_token)
    assert beta_client.get(
        "/memory", params={"agent": "keeper"}).status_code == 404
    monkeypatch.setenv("NEXUS_OPERATOR_TOKEN", "op-local-tok")
    local = _client("op-local-tok")
    assert len(local.get(
        "/memory", params={"agent": "keeper"}).json()["memories"]) == 1
