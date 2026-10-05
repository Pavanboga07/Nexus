"""Schedules: cron math, persistence, idempotent firing, autonomy gates."""

from __future__ import annotations

import asyncio
import datetime
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


SECRET = "schedule-test-secret-xxxxxxxxxxxx"
CALLS: list = []


async def _fake_execute(tools, name, args):
    CALLS.append((name, dict(args)))
    return json.dumps({"result": "scheduled-ok"})


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
    agents.create_agent(conn, SECRET, "chron")
    capabilities.register_capability(
        conn, "chron", "tick", tool="web_search")
    service.create_rule(conn, peer="*", action="*")
    yield conn
    conn.close()


def _run(coro):
    return asyncio.run(coro)


def test_cron_parser_cases():
    from app import autonomy

    assert autonomy.next_cron_fire(
        "* * * * *",
        datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc),
    ) == datetime.datetime(2026, 1, 1, 0, 1,
                            tzinfo=datetime.timezone.utc)
    assert autonomy.next_cron_fire(
        "0 8 * * *",
        datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc),
    ).hour == 8
    assert autonomy.next_cron_fire(
        "*/15 * * * *",
        datetime.datetime(2026, 1, 1, 0, 7, tzinfo=datetime.timezone.utc),
    ).minute == 15
    assert autonomy.next_cron_fire(
        "0 0 * * 1",
        datetime.datetime(2026, 1, 7, 0, 0, tzinfo=datetime.timezone.utc),
    ).weekday() == 0
    with pytest.raises(autonomy.AutonomyError):
        autonomy.next_cron_fire("not a cron",
                                datetime.datetime.now(datetime.timezone.utc))
    with pytest.raises(autonomy.AutonomyError):
        autonomy.next_cron_fire("61 * * * *",
                                datetime.datetime.now(datetime.timezone.utc))


def test_create_schedule_validates(world):
    from app import autonomy

    conn = world
    with pytest.raises(autonomy.AutonomyError):
        autonomy.create_schedule(conn, agent_id="ghost", kind="cron",
                                 trigger="* * * * *")
    with pytest.raises(autonomy.AutonomyError):
        autonomy.create_schedule(conn, agent_id="chron", kind="cron",
                                 trigger="bogus")
    with pytest.raises(autonomy.AutonomyError):
        autonomy.create_schedule(conn, agent_id="chron", kind="weekly",
                                 trigger="x")
    with pytest.raises(autonomy.AutonomyError):
        autonomy.create_schedule(conn, agent_id="chron", kind="once",
                                 trigger="not-a-time")


def test_once_schedule_fires_once(world):
    from app import autonomy

    conn = world
    now = datetime.datetime(2026, 5, 1, 12, 0,
                            tzinfo=datetime.timezone.utc)
    schedule = autonomy.create_schedule(
        conn, agent_id="chron", name="once", kind="once",
        trigger="2026-05-01T12:00:00+00:00",
        target_agent="chron", capability="chron.tick@v1",
        task_input={"q": "go"}, now=now)
    assert schedule["next_run_at"] == "2026-05-01T12:00:00+00:00"
    fired = _run(autonomy.scheduler_tick(conn, now=now))
    assert len(fired) == 1
    assert fired[0]["origin"] == "schedule"
    assert fired[0]["status"] == "COMPLETED"
    assert CALLS and CALLS[0][0] == "web_search"
    # Second tick: one-shot parked in the past, never refires.
    assert _run(autonomy.scheduler_tick(conn, now=now)) == []


def test_cron_advances_and_recovers_without_dupes(world):
    from app import autonomy
    from app import tasks as task_tracker

    conn = world
    now = datetime.datetime(2026, 5, 1, 12, 0,
                            tzinfo=datetime.timezone.utc)
    schedule = autonomy.create_schedule(
        conn, agent_id="chron", name="hourly", kind="cron",
        trigger="0 * * * *", target_agent="chron",
        capability="chron.tick@v1", now=now)
    assert schedule["next_run_at"] == "2026-05-01T13:00:00+00:00"
    # Simulate restart between ticks: same fire must not duplicate.
    first = _run(autonomy.scheduler_tick(
        conn, now=datetime.datetime(
            2026, 5, 1, 13, 0, 1, tzinfo=datetime.timezone.utc)))
    assert len(first) == 1
    key = first[0]["idempotency_key"]
    assert key.startswith(f"sched:{schedule['id']}:")
    second = _run(autonomy.scheduler_tick(
        conn, now=datetime.datetime(
            2026, 5, 1, 13, 0, 2, tzinfo=datetime.timezone.utc)))
    assert second == []
    rows = task_tracker.list_tasks(conn)
    assert sum(1 for t in rows if t["idempotency_key"] == key) == 1


def test_disabled_schedule_and_revoked_agent(world):
    from app import agents, autonomy

    conn = world
    now = datetime.datetime(2026, 5, 1, 12, 0,
                            tzinfo=datetime.timezone.utc)
    schedule = autonomy.create_schedule(
        conn, agent_id="chron", name="off", kind="once",
        trigger="2026-05-01T12:00:00+00:00",
        target_agent="chron", capability="chron.tick@v1", now=now)
    autonomy.set_schedule_enabled(conn, schedule["id"], False)
    assert _run(autonomy.scheduler_tick(conn, now=now)) == []
    autonomy.set_schedule_enabled(conn, schedule["id"], True)
    agents.set_agent_status(conn, "chron", "disabled")
    assert _run(autonomy.scheduler_tick(conn, now=now)) == []
    assert CALLS == []


def test_approval_gated_autonomy_parks(world):
    from app import autonomy

    conn = world
    now = datetime.datetime(2026, 5, 1, 12, 0,
                            tzinfo=datetime.timezone.utc)
    schedule = autonomy.create_schedule(
        conn, agent_id="chron", name="gated", kind="once",
        trigger="2026-05-01T12:00:00+00:00",
        target_agent="chron", capability="chron.tick@v1", now=now)
    conn.execute(
        "UPDATE agents SET autonomy = 'approval_required'"
        " WHERE id = 'chron'")
    conn.commit()
    fired = _run(autonomy.scheduler_tick(conn, now=now))
    assert len(fired) == 1
    assert fired[0]["status"] == "WAITING_APPROVAL"
    assert fired[0]["approval_id"]
    assert CALLS == []


def test_schedule_http_crud(db_env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import auth_headers

    client = TestClient(app, headers=auth_headers())
    client.post("/agents", json={"name": "sched"})
    created = client.post(
        "/autonomy/schedules",
        json={"agent_id": "sched", "name": "s",
              "kind": "once",
              "trigger": "2030-01-01T00:00:00+00:00"}).json()
    assert created["next_run_at"].startswith("2030")
    assert client.get(
        f"/autonomy/schedules/{created['id']}").json()["id"] == created["id"]
    listed = client.get("/autonomy/schedules").json()["schedules"]
    assert [s["id"] for s in listed] == [created["id"]]
    assert client.post(
        f"/autonomy/schedules/{created['id']}/disable").json()["enabled"] is False
    assert client.delete(
        f"/autonomy/schedules/{created['id']}").json()["deleted"] is True
    assert client.get("/autonomy/schedules").json() == {"schedules": []}
