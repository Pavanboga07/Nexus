"""End-to-end autonomy: schedule → trigger → workflow → remote agent.

Uses the real relay (uvicorn + Postgres), real signing/policy/
delegation, and two separate Nexus databases. A restart is simulated
mid-flight (reconcile + recover) and the run still converges exactly
once. Tool *results* are stubbed (no network in tests); every
authorization and transport hop is genuine.
"""

from __future__ import annotations

import asyncio
import datetime
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


SECRET_A = "e2e-a-secret-xxxxxxxxxxxxxxxxxxxx"
SECRET_B = "e2e-b-secret-xxxxxxxxxxxxxxxxxxxx"


async def _fake_execute(tools, name, args):
    return json.dumps({"result": "ok-%s" % name})


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def relay_server():
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
            if httpx.get(f"http://127.0.0.1:{port}/health",
                         timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.3)
    else:
        raise RuntimeError("relay did not start")
    yield f"ws://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=15)


def _open(path):
    from app.store import migrate, open_db

    conn = open_db(str(path))
    migrate(conn)
    return conn


def _run(coro, timeout=90):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _pub(conn, agent_id):
    row = conn.execute(
        "SELECT public_key FROM agent_identities WHERE agent_id = ?"
        " AND status = 'active' ORDER BY version DESC LIMIT 1",
        (agent_id,)).fetchone()
    return str(row["public_key"])


def test_scheduled_remote_workflow_with_restart(
        relay_engine, db_env, monkeypatch, relay_server):
    import datetime

    from app import agents, autonomy, capabilities, delegations, pairing
    from app.a2a import relay_client, service
    from app import tasks as task_tracker
    from app import workflows as flow_mod
    from app.identity import crypto

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    conn_a = _open(db_env / "a.db")
    conn_b = _open(db_env / "b.db")
    try:
        # A acts through the real machine secret (like production);
        # B keeps an independent test secret (separate host).
        from app.machine_config import get_or_create_identity_secret

        machine_secret = get_or_create_identity_secret()
        manager = agents.create_agent(conn_a, machine_secret, "manager")
        research = agents.create_agent(conn_b, SECRET_B, "research")
        capabilities.register_capability(
            conn_a, "manager", "initiate", tool="web_search")
        capabilities.register_capability(
            conn_a, "manager", "combine", tool="web_search")
        cap = capabilities.register_capability(
            conn_b, "research", "web_search", tool="web_search")
        service.create_rule(conn_a, peer="*", action="*")
        # Execution-scoped ALLOW on B; inbound requests still ASK so
        # the owner approval step is exercised.
        service.create_rule(
            conn_b, peer="*", action="research.web_search@v1")

        mgr_card = capabilities.agent_card(
            conn_a, machine_secret, "manager")
        res_card = capabilities.agent_card(conn_b, SECRET_B, "research")
        pairing.approve_peer(conn_b, mgr_card)
        pairing.approve_peer(conn_a, res_card)
        mgr_priv, _ = agents.resolve_signing_key(
            conn_a, machine_secret, "manager")
        res_priv, _ = agents.resolve_signing_key(
            conn_b, SECRET_B, "research")

        # Delegation up front (owner-driven setup, then autonomous).
        grant = delegations.issue_grant(
            conn_a, machine_secret, "manager", research["agent_id"],
            cap["id"], "answer", ttl_seconds=3600)
        step_grant = grant
        # Workflow: remote research, then local combine.
        flow = flow_mod.create_workflow(conn_a, name="morning", steps=[
            {"key": "research", "target_agent": research["agent_id"],
             "capability": cap["id"], "input": {"question": "AI news"},
             "delegation_id": step_grant["id"]},
            {"key": "combine", "target_agent": "manager",
             "capability": "manager.combine@v1", "input": {},
             "depends_on": ["research"]},
        ])
        # Schedule fires the initiator; its completion starts the flow.
        now = datetime.datetime.now(datetime.timezone.utc)
        autonomy.create_schedule(
            conn_a, agent_id="manager", name="morning", kind="once",
            trigger=(now + datetime.timedelta(seconds=1)).isoformat(),
            target_agent="manager",
            capability="manager.initiate@v1",
            task_input={"workflow": flow["id"]}, now=now)
        autonomy.create_trigger(
            conn_a, agent_id="manager", event_type="task.completed",
            target_workflow_id=flow["id"],
            event_filter={"capability_id": "manager.initiate@v1"})

        tick_at = now + datetime.timedelta(seconds=5)
        fired = _run(autonomy.scheduler_tick(conn_a, now=tick_at))
        assert len(fired) == 1 and fired[0]["status"] == "COMPLETED"

        # Simulated restart: reconcile + recover, nothing lost/duplicated.
        reconciled = task_tracker.reconcile_tasks(conn_a)
        assert reconciled == {"failed": []}
        recovered = flow_mod.recover_workflows(
            lambda: _open(db_env / "a.db"))
        assert recovered == []
        started = _run(autonomy.process_events(
            conn_a, conn_factory=lambda: _open(db_env / "a.db")))
        assert [w["workflow_id"] for w in started
                if "workflow_id" in w] == [flow["id"]]

        # Remote step dispatched: ferry it to B over the real relay.
        live = flow_mod.get_workflow(conn_a, flow["id"])
        remote = [s for s in live["steps"]
                  if s["step_key"] == "research"][0]
        assert remote["status"] == "DISPATCHED"
        rtask = task_tracker.get_task(conn_a, remote["task_id"])

        async def _to_b():
            ws_b = await relay_client.connect(
                relay_server + "/ws", agent_id=research["agent_id"],
                public_key_b64=_pub(conn_b, research["agent_id"]),
                sign_fn=lambda d: crypto.sign_bytes(res_priv, d))
            try:
                asked = service.create_request(
                    conn_a, signer_priv=mgr_priv,
                    sender_id=manager["agent_id"],
                    recipient_id=research["agent_id"],
                    question="AI news",
                    correlation_id=rtask["correlation_id"],
                    payload_extra={
                        "task_id": rtask["task_id"],
                        "capability_id": cap["id"],
                        "delegation_id": grant["id"]},
                    message_id="msg_e2e_" + rtask["task_id"][-6:],
                )
                status = await relay_client.deliver_one(
                    relay_server + "/ws", asked,
                    recipient=research["agent_id"],
                    agent_id=manager["agent_id"],
                    public_key_b64=_pub(conn_a, manager["agent_id"]),
                    sign_fn=lambda d: crypto.sign_bytes(mgr_priv, d),
                    ack_timeout=20.0)
                frame = await asyncio.wait_for(
                    ws_b.receive_json(), timeout=20)
                await relay_client.ack_delivery(ws_b, frame["relay_id"])
                return status, frame["envelope"]
            finally:
                await ws_b.close()

        _, inbound = _run(_to_b())
        parked = service.receive_envelope(
            conn_b, inbound, signer_priv=res_priv,
            local_id=research["agent_id"])
        assert parked["outcome"] == "parked"
        service.approve_approval(
            conn_b, parked["approval"]["approval_id"],
            signer_priv=res_priv, local_id=research["agent_id"])
        delegations.receive_grant(conn_b, dict(
            {k: v for k, v in grant.items()}))

        from app.execution import execute_capability

        out = _run(execute_capability(
            conn_b, acting_ref="research", capability_id=cap["id"],
            args={"query": "AI news"}, requester_ref="manager",
            purpose="answer", delegation_id=grant["id"],
            task_id=rtask["correlation_id"]))
        answered = service.send_response(
            conn_b, signer_priv=res_priv, sender_id=research["agent_id"],
            recipient_id=manager["agent_id"],
            correlation_id=rtask["correlation_id"],
            answer="news", remote_task_id="b-e2e-9")

        async def _backhaul():
            ws_a = await relay_client.connect(
                relay_server + "/ws", agent_id=manager["agent_id"],
                public_key_b64=_pub(conn_a, manager["agent_id"]),
                sign_fn=lambda d: crypto.sign_bytes(mgr_priv, d))
            try:
                await relay_client.deliver_one(
                    relay_server + "/ws", answered,
                    recipient=manager["agent_id"],
                    agent_id=research["agent_id"],
                    public_key_b64=_pub(conn_b, research["agent_id"]),
                    sign_fn=lambda d: crypto.sign_bytes(res_priv, d),
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

        # Re-enter the workflow: the deferred combine step picks up now
        # that its dependency completed (no work is duplicated).
        resumed = _run(flow_mod.run_workflow(
            lambda: _open(db_env / "a.db"), flow["id"],
            requesting_agent="manager"))
        assert resumed["status"] == "COMPLETED"

        # Workflow converges: remote done, combine ran after it.
        closed = flow_mod.get_workflow(conn_a, flow["id"])
        by_key = {s["step_key"]: s for s in closed["steps"]}
        assert by_key["research"]["status"] == "COMPLETED"
        assert closed["status"] == "COMPLETED"
        finished = task_tracker.get_task(conn_a, rtask["task_id"])
        assert finished["remote_task_id"] == "b-e2e-9"
        assert out["result"] == {"result": "ok-web_search"}
    finally:
        conn_a.close()
        conn_b.close()
