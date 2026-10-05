"""Multi-agent integration: manager → research across two databases.

One owner, two hosts (two SQLite files, two secrets): manager and
research are mutually paired like real laptops, then run the full
loop over the real relay queue (PostgreSQL) — discover, delegate,
ask, approve, execute under the grant, answer back signed. A third
agent (coding) sits out. Skips gracefully without PG.
"""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "unused.db"))
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return tmp_path


SECRET_MGR = "integration-mgr-secret-xxxxxxxxxx"
SECRET_RES = "integration-res-secret-xxxxxxxxxx"


async def _fake_execute(tools, name, args):
    return json.dumps({"answer": "moss grows on the north side"})


def _open(tmp_path, name):
    from app.store import migrate, open_db

    conn = open_db(str(tmp_path / name))
    migrate(conn)
    return conn


def _relay_hop(Session, envelope):
    from relay import queue
    from tests import relay_db

    async def main():
        async with Session() as s:
            status = await queue.enqueue(s, envelope=dict(envelope))
            await s.commit()
        async with Session() as s:
            rows = await queue.claim_for_recipient(
                s, envelope["recipient"], limit=10
            )
            await s.commit()
        assert status in ("queued", "dedup_hit")
        assert [r["message_id"] for r in rows] == [envelope["message_id"]]
        async with Session() as s:
            assert await queue.ack_delivered(s, envelope["message_id"]) is True
            await s.commit()

    relay_db.run(main())


def test_manager_research_loop(relay_engine, db_env, monkeypatch):
    from app import agents, capabilities, delegations, discovery
    from app.a2a import service
    from app.execution import execute_capability
    from app.memory.store import MemoryStore
    from app import pairing
    from relay.db import make_session_factory

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    Session = make_session_factory(relay_engine)
    mgr_db, res_db = _open(db_env, "mgr.db"), _open(db_env, "res.db")

    try:
        # Two hosts: distinct agents, keypairs, secrets, memories.
        manager = agents.create_agent(mgr_db, SECRET_MGR, "manager")
        research = agents.create_agent(res_db, SECRET_RES, "research")
        coding = agents.create_agent(res_db, SECRET_RES, "coding")
        assert manager["agent_id"] != research["agent_id"]
        assert manager["fingerprint"] != research["fingerprint"]

        mem_mgr = MemoryStore(str(db_env / "mgr.db"))
        mem_res = MemoryStore(str(db_env / "res.db"))
        mem_mgr.add("manager likes concise briefs")
        mem_res.add("moss grows on north sides")
        assert [m["text"] for m in mem_mgr.list_all()] == [
            "manager likes concise briefs"]
        assert [m["text"] for m in mem_res.list_all()] != [
            "manager likes concise briefs"]
        mem_mgr.close()
        mem_res.close()

        cap = capabilities.register_capability(
            res_db, "research", "web_search", tool="web_search")
        # Mutual pairing, exactly like two laptops (cards advertise caps).
        mgr_card = capabilities.agent_card(mgr_db, SECRET_MGR, "manager")
        res_card = capabilities.agent_card(res_db, SECRET_RES, "research")
        assert [c["id"] for c in res_card["capabilities"]] == [cap["id"]]
        pairing.approve_peer(res_db, mgr_card)
        pairing.approve_peer(mgr_db, res_card)

        # Research policy: only ITS capability is allowed.
        service.create_rule(
            res_db, peer="*", action="research.web_search@v1",
            agent="research")
        assert service.evaluate_policy(
            res_db, peer=manager["agent_id"], data_category="general",
            purpose="answer", action="research.web_search@v1",
            agent="research") == "ALLOW"
        assert service.evaluate_policy(
            res_db, peer=manager["agent_id"], data_category="general",
            purpose="answer", action="research.web_search@v1",
            agent="coding") == "ASK"

        # Manager discovers research by capability on its own host.
        found = discovery.resolve(mgr_db, res_card["agent_id"])
        assert found["explicit"] is True

        # Delegate the task, then ask over the relay queue.
        grant = delegations.issue_grant(
            mgr_db, SECRET_MGR, "manager", research["agent_id"],
            cap["id"], "answer", task_id="corr_demo001", ttl_seconds=3600)
        mgr_priv, _ = agents.resolve_signing_key(mgr_db, SECRET_MGR, "manager")
        asked = service.create_request(
            mgr_db, signer_priv=mgr_priv, sender_id=manager["agent_id"],
            recipient_id=research["agent_id"],
            question="which side does moss grow on?",
            correlation_id="corr_demo001",
            payload_extra={"capability_id": cap["id"],
                           "delegation_id": grant["id"],
                           "target_agent": research["agent_id"]},
        )
        assert asked["payload"]["delegation_id"] == grant["id"]
        _relay_hop(Session, asked)

        res_priv, _ = agents.resolve_signing_key(
            res_db, SECRET_RES, "research")
        parked = service.receive_envelope(
            res_db, asked, signer_priv=res_priv,
            local_id=research["agent_id"])
        assert parked["outcome"] == "parked"

        # Owner approves on research's host; research executes the grant.
        card = parked["approval"]
        service.approve_approval(
            res_db, card["approval_id"], signer_priv=res_priv,
            local_id=research["agent_id"])
        delegations.receive_grant(res_db, dict(grant))
        out = asyncio.run(execute_capability(
            res_db, acting_ref="research", capability_id=cap["id"],
            args={"query": "moss"}, requester_ref="manager",
            purpose="answer", delegation_id=grant["id"],
            task_id="corr_demo001"))
        assert out["result"] == {"answer": "moss grows on the north side"}

        # Signed answer travels back; manager verifies and accepts.
        answered = service.send_response(
            res_db, signer_priv=res_priv, sender_id=research["agent_id"],
            recipient_id=manager["agent_id"],
            correlation_id="corr_demo001",
            answer="moss grows on the north side")
        _relay_hop(Session, answered)
        done = service.receive_envelope(
            mgr_db, answered, signer_priv=mgr_priv,
            local_id=manager["agent_id"])
        assert done["outcome"] == "answered"

        # Coding sat out: nothing pending, nothing executed for it.
        assert service.list_approvals(res_db, status="pending") == []
    finally:
        mgr_db.close()
        res_db.close()
