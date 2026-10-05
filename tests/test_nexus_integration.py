"""Two-Nexus integration over a real relay: nothing mocked on the path.

Nexus A (manager) and Nexus B (research) are separate DBs, secrets,
and keypairs; the relay is a real uvicorn server with real Postgres.
Every hop below is genuine: challenge auth, signed envelopes, pairing
trust, capability + delegation + policy checks, queue/ack transport,
task completion, and audit. (Tool *results* are stubbed — no network
in tests — but authorization around them is fully exercised.)
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "unused.db"))
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return tmp_path


SECRET_A = "nexus-a-secret-xxxxxxxxxxxxxxxxxx"
SECRET_B = "nexus-b-secret-xxxxxxxxxxxxxxxxxx"


async def _fake_execute(tools, name, args):
    return json.dumps({"answer": "moss grows north"})


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def relay_server():
    """Real relay app on a real port with the shared test Postgres."""
    import httpx
    import uvicorn

    from relay.main import create_relay_app
    from tests.relay_db import TEST_URL

    port = _free_port()
    app = create_relay_app(database_url=TEST_URL, ack_timeout=10.0)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health",
                             timeout=2)
            if resp.status_code == 200:
                break
        except Exception:
            time.sleep(0.3)
    else:
        raise RuntimeError("relay did not start")
    yield f"ws://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=15)


def _open_db(path):
    from app.store import migrate, open_db

    conn = open_db(str(path))
    migrate(conn)
    return conn


def _run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_two_nexus_task_loop(relay_engine, db_env, monkeypatch,
                             relay_server):
    from app import agents, capabilities, delegations, discovery
    from app.a2a import relay_client, service
    from app import pairing
    from app.identity import crypto

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    conn_a = _open_db(db_env / "a.db")
    conn_b = _open_db(db_env / "b.db")
    try:
        manager = agents.create_agent(conn_a, SECRET_A, "manager")
        research = agents.create_agent(conn_b, SECRET_B, "research")
        assert manager["agent_id"] != research["agent_id"]
        cap = capabilities.register_capability(
            conn_b, "research", "web_search", tool="web_search")
        # Execution scoped ALLOW; inbound requests still ASK → approval.
        service.create_rule(
            conn_b, peer="*", action="research.web_search@v1")

        # Mutual pairing via signed cards, exactly like two laptops.
        mgr_card = capabilities.agent_card(
            conn_a, SECRET_A, "manager")
        res_card = capabilities.agent_card(
            conn_b, SECRET_B, "research")
        pairing.approve_peer(conn_b, mgr_card)
        pairing.approve_peer(conn_a, res_card)

        # A discovers B's capability through the paired card.
        found = discovery.resolve(conn_a, cap["id"])
        assert found["explicit"] is True
        assert found["matches"][0]["id"] == research["agent_id"]

        # A delegates + dispatches through the orchestrator.
        from app import orchestration, tasks
        from app.a2a import service as service_a

        service_a.create_rule(conn_a, peer="*", action="*")
        mgr_priv, _ = agents.resolve_signing_key(
            conn_a, SECRET_A, "manager")
        grant = delegations.issue_grant(
            conn_a, SECRET_A, "manager", research["agent_id"],
            cap["id"], "answer", ttl_seconds=3600)
        task = tasks.create_task(
            conn_a, requesting_agent_id=manager["id"],
            target_agent=research["agent_id"],
            capability=cap["id"],
            task_input={"question": "moss?"},
            delegation_id=grant["id"])
        final, envelope = _run(orchestration.run_task(
            conn_a, task["task_id"], secret=SECRET_A))
        assert final["status"] == "DISPATCHED", final
        assert envelope is not None

        # B listens on the real relay; A delivers for real.
        res_priv, _ = agents.resolve_signing_key(
            conn_b, SECRET_B, "research")

        async def _roundtrip():
            ws_b = await relay_client.connect(
                relay_server + "/ws", agent_id=research["agent_id"],
                public_key_b64=active_pub(conn_b),
                sign_fn=lambda data: crypto.sign_bytes(res_priv, data))
            try:
                status = await relay_client.deliver_one(
                    relay_server + "/ws", envelope,
                    recipient=research["agent_id"],
                    agent_id=manager["agent_id"],
                    public_key_b64=active_pub(conn_a),
                    sign_fn=lambda data: crypto.sign_bytes(mgr_priv, data),
                    ack_timeout=20.0,
                )
                frame = await asyncio.wait_for(
                    ws_b.receive_json(), timeout=20)
                assert frame["type"] == "delivery"
                await relay_client.ack_delivery(ws_b, frame["relay_id"])
                return status, frame["envelope"]
            finally:
                await ws_b.close()

        status, inbound = _run(_roundtrip())
        assert status in ("delivered", "relayed", "queued")

        # B ingests, approves, executes under the grant, answers back.
        parked = service.receive_envelope(
            conn_b, inbound, signer_priv=res_priv,
            local_id=research["agent_id"])
        assert parked["outcome"] == "parked"
        service.approve_approval(
            conn_b, parked["approval"]["approval_id"],
            signer_priv=res_priv, local_id=research["agent_id"])
        delegations.receive_grant(conn_b, dict(_stored_grant(
            conn_a, grant["id"])))
        from app.execution import execute_capability

        out = _run(execute_capability(
            conn_b, acting_ref="research", capability_id=cap["id"],
            args={"query": "moss"}, requester_ref="manager",
            purpose="answer", delegation_id=grant["id"],
            task_id=final["correlation_id"]))
        assert out["result"] == {"answer": "moss grows north"}
        answered = service.send_response(
            conn_b, signer_priv=res_priv, sender_id=research["agent_id"],
            recipient_id=manager["agent_id"],
            correlation_id=final["correlation_id"],
            answer="moss grows north", remote_task_id="b-task-7")

        async def _backhaul():
            ws_a = await relay_client.connect(
                relay_server + "/ws", agent_id=manager["agent_id"],
                public_key_b64=active_pub(conn_a),
                sign_fn=lambda data: crypto.sign_bytes(mgr_priv, data))
            try:
                await relay_client.deliver_one(
                    relay_server + "/ws", answered,
                    recipient=manager["agent_id"],
                    agent_id=research["agent_id"],
                    public_key_b64=active_pub(conn_b),
                    sign_fn=lambda data: crypto.sign_bytes(res_priv, data),
                    ack_timeout=20.0)
                frame = await asyncio.wait_for(
                    ws_a.receive_json(), timeout=20)
                await relay_client.ack_delivery(ws_a, frame["relay_id"])
                return frame["envelope"]
            finally:
                await ws_a.close()

        back = _run(_backhaul())
        done = service.receive_envelope(
            conn_a, back, signer_priv=mgr_priv,
            local_id=manager["agent_id"])
        assert done["outcome"] == "answered"
        finished = tasks.get_task(conn_a, final["task_id"])
        assert finished["status"] == "COMPLETED"
        assert finished["remote_task_id"] == "b-task-7"
        events = [e["event"] for e in tasks.task_events(
            conn_a, final["task_id"])]
        assert "TASK_CREATED" in events and "TASK_COMPLETED" in events
    finally:
        conn_a.close()
        conn_b.close()


def active_pub(conn):
    row = conn.execute(
        "SELECT public_key FROM agent_identities WHERE status = 'active'"
        " ORDER BY version DESC LIMIT 1").fetchone()
    return str(row["public_key"])


def _stored_grant(conn, grant_id):
    row = conn.execute(
        "SELECT * FROM delegations WHERE id = ?", (grant_id,)).fetchone()
    out = dict(row)
    import json as _json

    out["constraints"] = _json.loads(out.pop("constraints_json"))
    return out
