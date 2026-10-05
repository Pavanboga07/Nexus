"""Workflows: DAG validation, sequential/parallel runs, failure, cancel."""

from __future__ import annotations

import asyncio
import json
import os

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


SECRET = "workflow-test-secret-xxxxxxxxxxxx"
CALLS: list = []


async def _fake_execute(tools, name, args):
    CALLS.append((name, dict(args)))
    await asyncio.sleep(0)
    return json.dumps({"result": f"did {name}:{args.get('q', '')}"})


def _conn_factory(db):
    from app.store import migrate, open_db

    def make():
        conn = open_db(db)
        migrate(conn)
        return conn

    return make


@pytest.fixture
def world(db_env, monkeypatch):
    from app import agents, capabilities
    from app.a2a import service

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    CALLS.clear()
    factory = _conn_factory(db_env)
    conn = factory()
    agents.create_agent(conn, SECRET, "research")
    agents.create_agent(conn, SECRET, "coding")
    capabilities.register_capability(
        conn, "research", "web_search", tool="web_search")
    capabilities.register_capability(
        conn, "coding", "review", tool="web_fetch")
    service.create_rule(conn, peer="*", action="*")
    conn.close()
    return factory


def _run(coro):
    return asyncio.run(coro)


def _steps(*specs):
    return [
        {"key": key, "target_agent": agent, "capability": cap,
         "input": {"q": key}}
        for key, agent, cap in specs
    ]


def test_validate_workflow_definition(world):
    from app import workflows

    conn = world()
    try:
        for bad in ([], [{"key": ""}], [{"key": "a"}, {"key": "a"}],
                    [{"key": "a", "depends_on": ["ghost"]}],
                    [{"key": "a", "depends_on": ["b"]},
                     {"key": "b", "depends_on": ["a"]}]):
            with pytest.raises(workflows.WorkflowError):
                workflows.create_workflow(conn, name="w", steps=bad)
    finally:
        conn.close()


def test_sequential_workflow_completes(world):
    from app import workflows

    conn = world()
    try:
        flow = workflows.create_workflow(
            conn, name="seq",
            steps=_steps(("one", "research", "research.web_search@v1"),
                         ("two", "coding", "coding.review@v1")))
        # Wire the dependency: two waits for one.
        conn.execute(
            "UPDATE workflow_steps SET depends_on_json = ?"
            " WHERE workflow_id = ? AND step_key = 'two'",
            (json.dumps(["one"]), flow["id"]))
        conn.commit()
        done = _run(workflows.run_workflow(
            _conn_factory(os.environ["NEXUS_DB_PATH"]),
            flow["id"]))
        assert done["status"] == "COMPLETED"
        by_key = {s["step_key"]: s for s in done["steps"]}
        assert by_key["one"]["status"] == "COMPLETED"
        assert by_key["two"]["status"] == "COMPLETED"
        assert "did web_search" in by_key["one"]["result_json"]
    finally:
        conn.close()


def test_parallel_fan_out_completes(world):
    from app import workflows

    conn = world()
    try:
        flow = workflows.create_workflow(
            conn, name="fan",
            steps=_steps(("a", "research", "research.web_search@v1"),
                         ("b", "coding", "coding.review@v1")))
        done = _run(workflows.run_workflow(
            _conn_factory(os.environ["NEXUS_DB_PATH"]), flow["id"]))
        assert done["status"] == "COMPLETED"
        assert {s["status"] for s in done["steps"]} == {"COMPLETED"}
        assert len(CALLS) == 2
    finally:
        conn.close()


def test_failed_step_blocks_dependent(world):
    from app import workflows

    conn = world()
    try:
        flow = workflows.create_workflow(
            conn, name="fail",
            steps=_steps(("bad", "research", "research.missing@v9"),
                         ("next", "coding", "coding.review@v1")))
        conn.execute(
            "UPDATE workflow_steps SET depends_on_json = ?"
            " WHERE workflow_id = ? AND step_key = 'next'",
            (json.dumps(["bad"]), flow["id"]))
        conn.commit()
        done = _run(workflows.run_workflow(
            _conn_factory(os.environ["NEXUS_DB_PATH"]), flow["id"]))
        assert done["status"] == "FAILED"
        by_key = {s["step_key"]: s for s in done["steps"]}
        assert by_key["bad"]["status"] == "FAILED"
        assert by_key["next"]["status"] == "BLOCKED"
    finally:
        conn.close()


def test_cancel_workflow(world):
    from app import workflows

    conn = world()
    try:
        flow = workflows.create_workflow(
            conn, name="cancel",
            steps=_steps(("a", "research", "research.web_search@v1")))
        out = workflows.cancel_workflow(
            _conn_factory(os.environ["NEXUS_DB_PATH"]), flow["id"])
        assert out["status"] == "CANCELLED"
        assert out["steps"][0]["status"] == "CANCELLED"
        with pytest.raises(workflows.WorkflowError):
            _run(workflows.run_workflow(
                _conn_factory(os.environ["NEXUS_DB_PATH"]), flow["id"]))
    finally:
        conn.close()


def test_workflow_http_round_trip(db_env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import auth_headers

    client = TestClient(app, headers=auth_headers())
    client.post("/agents", json={"name": "wresearch"})
    client.post("/agents/wresearch/capabilities",
                json={"name": "web_search", "tool": "web_search"})
    client.post("/ask/policy", json={"peer": "*", "action": "*"})
    resp = client.post(
        "/workflows/run",
        json={"name": "http",
              "steps": [{"key": "s1", "target_agent": "wresearch",
                         "capability": "wresearch.web_search@v1",
                         "input": {"query": "x"}}]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] in ("COMPLETED", "FAILED", "RUNNING")
    assert body["steps"][0]["task_id"]
    listed = client.get("/workflows").json()["workflows"]
    assert [w["id"] for w in listed] == [body["id"]]
    detail = client.get(f"/workflows/{body['id']}").json()
    assert detail["id"] == body["id"]
