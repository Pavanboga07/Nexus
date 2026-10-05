"""Recovery: reconcile after restart, workflow resume, no resurrections."""

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


SECRET = "recovery-test-secret-xxxxxxxxxxxx"


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


def test_reconcile_timeouts_and_worker_gone(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        timed = tasks.create_task(conn, target_agent="r", timeout_seconds=60)
        tasks.transition(conn, timed["task_id"], "RESOLVING")
        tasks.transition(conn, timed["task_id"], "AUTHORIZED")
        tasks.transition(conn, timed["task_id"], "DISPATCHED")
        conn.execute(
            "UPDATE tasks SET deadline_at = '2000-01-01T00:00:00Z'"
            " WHERE task_id = ?", (timed["task_id"],))
        conn.commit()
        stuck = tasks.create_task(conn, target_agent="r")
        tasks.transition(conn, stuck["task_id"], "RESOLVING")
        tasks.transition(conn, stuck["task_id"], "AUTHORIZED")
        tasks.transition(conn, stuck["task_id"], "DISPATCHED")
        tasks.transition(conn, stuck["task_id"], "RUNNING")
        report = tasks.reconcile_tasks(conn)
        assert set(report["failed"]) == {timed["task_id"],
                                         stuck["task_id"]}
        assert tasks.get_task(
            conn, timed["task_id"])["error_code"] == "TIMEOUT"
        assert tasks.get_task(
            conn, stuck["task_id"])["error_code"] == "WORKER_GONE"
    finally:
        conn.close()


def test_reconcile_approval_expiry(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = tasks.create_task(conn, target_agent="r")
        for state in ("RESOLVING", "AUTHORIZED"):
            tasks.transition(conn, task["task_id"], state)
        conn.execute(
            "INSERT INTO a2a_approvals (approval_id, message_id,"
            " correlation_id, requester, action, data_category, purpose,"
            " question, expires_at, status, decided_at, created_at)"
            " VALUES ('apr_old', 'msg_old', 'corr_old', 'x', 'answer',"
            " 'general', 'answer', 'q', '2000-01-01T00:00:00Z',"
            " 'pending', '', '')")
        conn.execute(
            "UPDATE tasks SET status = 'WAITING_APPROVAL',"
            " approval_id = 'apr_old' WHERE task_id = ?",
            (task["task_id"],))
        conn.commit()
        report = tasks.reconcile_tasks(conn)
        assert report["failed"] == [task["task_id"]]
        assert tasks.get_task(
            conn, task["task_id"])["status"] == "APPROVAL_EXPIRED"
    finally:
        conn.close()


def test_no_duplicate_completion(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = tasks.create_task(conn, target_agent="r")
        tasks.complete_task(conn, task["task_id"], {"ok": 1})
        with pytest.raises(tasks.TaskError):
            tasks.complete_task(conn, task["task_id"], {"ok": 2})
        assert tasks.get_task(
            conn, task["task_id"])["output"] == {"ok": 1}
    finally:
        conn.close()


def test_workflow_pause_resume_recover(db_env, monkeypatch):
    import asyncio
    import json

    from app import agents, capabilities, workflows
    from app.a2a import service

    async def _fake_execute(tools, name, args):
        return json.dumps({"result": "ok"})

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    import os

    factory_calls = []

    def factory():
        from app.store import migrate, open_db

        conn = open_db(os.environ["NEXUS_DB_PATH"])
        migrate(conn)
        factory_calls.append(1)
        return conn

    conn = factory()
    try:
        agents.create_agent(conn, SECRET, "doer")
        capabilities.register_capability(
            conn, "doer", "job", tool="web_search")
        service.create_rule(conn, peer="*", action="*")
        flow = workflows.create_workflow(conn, name="w", steps=[
            {"key": "a", "target_agent": "doer",
             "capability": "doer.job@v1", "input": {}},
            {"key": "b", "target_agent": "doer",
             "capability": "doer.job@v1", "input": {},
             "depends_on": ["a"]},
        ])
        paused = workflows.pause_workflow(
            lambda: factory(), flow["id"])
        assert paused["status"] == "PAUSED"
        done = asyncio.run(workflows.run_workflow(
            factory, flow["id"]))
        assert done["status"] == "COMPLETED"
        assert {s["status"] for s in done["steps"]} == {"COMPLETED"}
    finally:
        conn.close()


def test_workflow_crash_recovery(db_env, monkeypatch):
    import asyncio
    import json

    from app import agents, capabilities, workflows
    from app.a2a import service

    async def _fake_execute(tools, name, args):
        return json.dumps({"result": "ok"})

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    import os

    def factory():
        from app.store import migrate, open_db

        conn = open_db(os.environ["NEXUS_DB_PATH"])
        migrate(conn)
        return conn

    conn = factory()
    try:
        agents.create_agent(conn, SECRET, "doer")
        capabilities.register_capability(
            conn, "doer", "job", tool="web_search")
        service.create_rule(conn, peer="*", action="*")
        flow = workflows.create_workflow(conn, name="crash", steps=[
            {"key": "a", "target_agent": "doer",
             "capability": "doer.job@v1", "input": {}},
            {"key": "b", "target_agent": "doer",
             "capability": "doer.job@v1", "input": {},
             "depends_on": ["a"]},
        ])
        # Simulate a crash mid-run: step A completed, step B stuck.
        step_a = [s for s in flow["steps"] if s["step_key"] == "a"][0]
        step_b = [s for s in flow["steps"] if s["step_key"] == "b"][0]
        from app import tasks as task_tracker

        t_a = task_tracker.create_task(
            conn, target_agent="doer",
            capability="doer.job@v1")
        task_tracker.complete_task(conn, t_a["task_id"], {"ok": 1})
        t_b = task_tracker.create_task(
            conn, target_agent="doer",
            capability="doer.job@v1")
        with conn:
            conn.execute(
                "UPDATE workflow_steps SET status = 'RUNNING',"
                " task_id = ? WHERE step_id = ?",
                (t_b["task_id"], step_b["step_id"]))
            conn.execute(
                "UPDATE workflow_steps SET status = 'COMPLETED',"
                " task_id = ? WHERE step_id = ?",
                (t_a["task_id"], step_a["step_id"]))
            conn.execute(
                "UPDATE workflows SET status = 'RUNNING' WHERE id = ?",
                (flow["id"],))
        recovered = workflows.recover_workflows(factory)
        assert [f["id"] for f in recovered] == [flow["id"]]
        assert recovered[0]["status"] == "PENDING"
        done = asyncio.run(workflows.run_workflow(
            factory, flow["id"]))
        assert done["status"] == "COMPLETED"
    finally:
        conn.close()


def test_agent_reconcile_fails_live_work(db_env):
    from app import agents, orchestration

    conn = _conn(db_env)
    try:
        agents.create_agent(conn, SECRET, "doomed")
        from app import tasks

        live = tasks.create_task(conn, target_agent="doomed")
        agents.set_agent_status(conn, "doomed", "revoked")
        out = orchestration.reconcile_agent_tasks(conn, "doomed")
        assert out == {"failed": [live["task_id"]]}
        assert tasks.get_task(
            conn, live["task_id"])["error_code"] == "AGENT_REVOKED"
        # Active agents are untouched.
        agents.create_agent(conn, SECRET, "fine")
        assert orchestration.reconcile_agent_tasks(
            conn, "fine") == {"failed": []}
    finally:
        conn.close()
