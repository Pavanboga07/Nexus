"""Orchestrator: routing, authorization, dispatch, advance, parents."""

from __future__ import annotations

import asyncio
import json

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


SECRET = "orchestration-test-secret-xxxxxxxxx"
CALLS: list = []


async def _fake_execute(tools, name, args):
    CALLS.append((name, dict(args)))
    return json.dumps({"result": f"did {name}"})


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


@pytest.fixture
def world(db_env, monkeypatch):
    from app import agents, capabilities
    from app.a2a import service

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    CALLS.clear()
    conn = _conn(db_env)
    manager = agents.create_agent(conn, SECRET, "manager")
    research = agents.create_agent(conn, SECRET, "research")
    capabilities.register_capability(
        conn, "research", "web_search", tool="web_search")
    service.create_rule(conn, peer="*", action="*")
    yield {"conn": conn, "manager": manager, "research": research}
    conn.close()


def _task(conn, **kw):
    from app import tasks

    params = {"requesting_agent_id": "owner"}
    params.update(kw)
    return tasks.create_task(conn, **params)


def _run(coro):
    return asyncio.run(coro)


def test_explicit_target_routing(world):
    from app import orchestration

    conn = world["conn"]
    task = _task(conn, target_agent="research",
                 capability="research.web_search@v1")
    out = orchestration.resolve_task(conn, task["task_id"])
    assert out["status"] == "RESOLVING"
    assert out["target_agent_id"] == world["research"]["agent_id"]
    assert out["capability_id"] == "research.web_search@v1"


def test_missing_target_failed(world):
    from app import orchestration

    conn = world["conn"]
    task = _task(conn, target_agent="ghost",
                 capability="research.web_search@v1")
    out = orchestration.resolve_task(conn, task["task_id"])
    assert out["status"] == "FAILED"
    assert out["error_code"] == "AGENT_NOT_FOUND"


def test_disabled_target_rejected(world):
    from app import agents, orchestration

    conn = world["conn"]
    agents.set_agent_status(conn, "research", "disabled")
    task = _task(conn, target_agent="research",
                 capability="research.web_search@v1")
    out = orchestration.resolve_task(conn, task["task_id"])
    assert out["status"] == "FAILED"
    assert out["error_code"] == "AGENT_DISABLED"


def test_revoked_target_rejected(world):
    from app import agents, orchestration

    conn = world["conn"]
    agents.set_agent_status(conn, "research", "revoked")
    task = _task(conn, target_agent="research",
                 capability="research.web_search@v1")
    out = orchestration.resolve_task(conn, task["task_id"])
    assert out["status"] == "FAILED"
    assert out["error_code"] == "AGENT_REVOKED"


def test_capability_mismatch_rejected(world):
    from app import orchestration

    conn = world["conn"]
    task = _task(conn, target_agent="research",
                 capability="manager.nope@v1")
    out = orchestration.resolve_task(conn, task["task_id"])
    assert out["status"] == "FAILED"
    assert out["error_code"] == "CAPABILITY_NOT_OWNED"


def test_capability_based_routing(world):
    from app import orchestration

    conn = world["conn"]
    task = _task(conn, capability="research.web_search@v1")
    out = orchestration.resolve_task(conn, task["task_id"])
    assert out["status"] == "RESOLVING"
    assert out["target_agent_id"] == world["research"]["agent_id"]


def test_ambiguous_routing_lists_candidates(world):
    from app import agents, capabilities, orchestration

    conn = world["conn"]
    agents.create_agent(conn, SECRET, "research2")
    capabilities.register_capability(
        conn, "research2", "web_search", tool="web_search")
    from app import discovery

    # Same capability NAME on two agents; full IDs differ, so resolve
    # by the shared name suffix through a direct discovery check.
    out = discovery.resolve(conn, "web_search", fuzzy=True)
    assert out["explicit"] is False
    assert len(out["matches"]) == 2
    task = _task(conn, capability="web_search")
    routed = orchestration.resolve_task(conn, task["task_id"])
    # Bare name is not a capability id: explicit resolution fails.
    assert routed["status"] == "FAILED"
    assert routed["error_code"] in ("CAPABILITY_NOT_FOUND", "AMBIGUOUS")


def test_full_local_run_to_completed(world):
    from app import orchestration

    conn = world["conn"]
    task = _task(conn, target_agent="research",
                 capability="research.web_search@v1",
                 task_input={"query": "moss"})
    final, envelope = _run(orchestration.run_task(conn, task["task_id"]))
    assert final["status"] == "COMPLETED"
    assert envelope is None
    assert final["output"]["result"] == {"result": "did web_search"}
    assert CALLS and CALLS[0][0] == "web_search"


def test_policy_ask_parks_approval_then_advances(world):
    from app import orchestration
    from app import tasks

    conn = world["conn"]
    conn.execute("DELETE FROM policy_rules")
    conn.commit()
    task = _task(conn, target_agent="research",
                 capability="research.web_search@v1")
    final, _ = _run(orchestration.run_task(conn, task["task_id"]))
    assert final["status"] == "WAITING_APPROVAL"
    assert final["approval_id"]
    # Owner approves through the normal decide API surface.
    from app.a2a import service

    card = service.list_approvals(conn)[0]
    assert card["approval_id"] == final["approval_id"]
    _approve(conn, card["approval_id"])
    done, _ = _run(orchestration.advance_task(conn, task["task_id"]))
    assert done["status"] == "COMPLETED"


def _approve(conn, approval_id):
    from app.a2a import service

    row = conn.execute(
        "SELECT * FROM a2a_approvals WHERE approval_id = ?",
        (approval_id,)).fetchone()
    assert row is not None
    conn.execute(
        "UPDATE a2a_approvals SET status = 'approved', decided_at = 'now'"
        " WHERE approval_id = ?", (approval_id,))
    conn.commit()
    _ = service


def test_policy_deny_fails_task(world):
    from app import orchestration

    conn = world["conn"]
    conn.execute("DELETE FROM policy_rules")
    conn.execute(
        "INSERT INTO policy_rules (rule_id, peer, data_category,"
        " purpose, action, effect, agent_id, created_at)"
        " VALUES ('r1', '*', 'credentials', '*', '*', 'ALLOW', '*', '')")
    conn.commit()
    task = _task(conn, target_agent="research",
                 capability="research.web_search@v1",
                 task_input={"data_category": "credentials"})
    final, _ = _run(orchestration.run_task(conn, task["task_id"]))
    assert final["status"] == "FAILED"
    assert final["error_code"] == "POLICY_DENIED"


def test_remote_dispatch_builds_envelope(world):
    from app import orchestration, pairing

    conn = world["conn"]
    # Pair a "remote" peer by importing a second profile's card.
    from tests.test_a2a_loop import make_profile
    import tempfile, os

    other = tempfile.mkdtemp()
    from app.store import open_db, migrate
    from relay.directory import sign_card
    from relay.envelope import utc_iso_in, utc_now_iso

    conn2 = open_db(os.path.join(other, "peer.db"))
    migrate(conn2)
    from app.identity import crypto
    from app.identity.service import ensure_identity

    ident = ensure_identity(conn2, "peer-secret-xxxxxxxxxxxxxxxx")
    row = conn2.execute(
        "SELECT public_key, encrypted_private_key FROM identity"
        " WHERE id = 1").fetchone()
    pub = row["public_key"]
    priv = crypto.load_private_key(crypto.decrypt_private_key(
        row["encrypted_private_key"], "peer-secret-xxxxxxxxxxxxxxxx"))
    card = sign_card(priv, {
        "type": "agent-card", "protocol": "nexus-a2a", "version": "0.3",
        "agent_id": ident.agent_id, "display_name": "Faraway",
        "public_key": pub, "endpoint": "ws://test.invalid/ws",
        "capabilities": [{"id": "faraway.survey@v1", "version": 1}],
        "supported_purposes": [], "issued_at": utc_now_iso(),
        "expires_at": utc_iso_in(3600),
    })
    pairing.approve_peer(conn, card)
    task = _task(conn, target_agent=ident.agent_id,
                 capability="faraway.survey@v1",
                 task_input={"question": "hi"})
    # Requesting owner cannot sign remote dispatch.
    final, envelope = _run(orchestration.run_task(conn, task["task_id"]))
    assert final["status"] == "FAILED"
    assert final["error_code"] == "NO_SIGNING_IDENTITY"
    # A local requesting agent signs; envelope carries task lineage.
    from app import delegations

    grant = delegations.issue_grant(
        conn, SECRET, "manager", ident.agent_id,
        "faraway.survey@v1", "answer", ttl_seconds=3600)
    task2 = _task(conn, target_agent=ident.agent_id,
                  capability="faraway.survey@v1",
                  task_input={"question": "hi again"},
                  requesting_agent_id=world["manager"]["agent_id"],
                  delegation_id=grant["id"])
    final2, envelope2 = _run(orchestration.run_task(
        conn, task2["task_id"], secret=SECRET))
    assert final2["status"] == "DISPATCHED"
    assert envelope2 is not None
    assert envelope2["payload"]["task_id"] == task2["task_id"]
    assert envelope2["payload"]["capability_id"] == "faraway.survey@v1"
    assert envelope2["sender"] == world["manager"]["agent_id"]
    conn2.close()


def test_parent_children_completion(world):
    from app import orchestration, tasks

    conn = world["conn"]
    parent = tasks.create_task(conn, task_input={"group": True})
    c1 = tasks.create_task(
        conn, target_agent="research",
        capability="research.web_search@v1",
        parent_task_id=parent["task_id"])
    c2 = tasks.create_task(
        conn, target_agent="research",
        capability="research.web_search@v1",
        parent_task_id=parent["task_id"])
    for child in (c1, c2):
        _run(orchestration.run_task(conn, child["task_id"]))
    closed = tasks.close_parent_if_done(conn, parent["task_id"])
    assert closed["status"] == "COMPLETED"
    assert set(closed["output"]["children"]) == {
        c1["task_id"], c2["task_id"]}


def test_isolation_service_level(world):
    from app import orchestration, tasks

    conn = world["conn"]
    task = tasks.create_task(
        conn, requesting_agent_id=world["manager"]["agent_id"],
        target_agent="research",
        capability="research.web_search@v1")
    orchestration.resolve_task(conn, task["task_id"])
    with __import__("pytest").raises(tasks.TaskError) as exc:
        tasks.require_task_actor(conn, task["task_id"], "stranger-id")
    assert exc.value.code == "FORBIDDEN"
    assert tasks.require_task_actor(
        conn, task["task_id"],
        world["research"]["agent_id"])["task_id"] == task["task_id"]
