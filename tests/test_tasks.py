"""Durable tasks: lifecycle, idempotency, timeout, retry, cancel, access."""

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


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


def _make(conn, **kw):
    from app import tasks

    params = {"target_agent": "research", "capability": "research.x@v1"}
    params.update(kw)
    return tasks.create_task(conn, **params)


def test_create_and_retrieve(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = _make(conn, task_input={"q": "hi"}, purpose="answer")
        assert task["status"] == "PENDING"
        assert task["input"] == {"q": "hi"}
        assert task["correlation_id"].startswith("corr_")
        assert task["retry_count"] == 0
        fetched = tasks.get_task(conn, task["task_id"])
        assert fetched["task_id"] == task["task_id"]
        assert tasks.list_tasks(conn)[0]["task_id"] == task["task_id"]
        with pytest.raises(tasks.TaskError) as exc:
            tasks.get_task(conn, "tsk_nope")
        assert exc.value.code == "NOT_FOUND"
    finally:
        conn.close()


def test_valid_transitions_walk(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = _make(conn)
        for state in ("RESOLVING", "AUTHORIZED", "DISPATCHED", "RUNNING"):
            task = tasks.transition(conn, task["task_id"], state)
            assert task["status"] == state
        done = tasks.complete_task(conn, task["task_id"], {"ok": True})
        assert done["status"] == "COMPLETED"
        assert done["output"] == {"ok": True}
        assert done["completed_at"]
        events = [e["event"] for e in tasks.task_events(conn, task["task_id"])]
        assert events[0] == "TASK_CREATED"
        assert "TASK_COMPLETED" in events
    finally:
        conn.close()


def test_invalid_transitions_rejected(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = _make(conn)
        with pytest.raises(tasks.TaskError) as exc:
            tasks.transition(conn, task["task_id"], "COMPLETED")
        assert exc.value.code == "BAD_TRANSITION"
        for state in ("RESOLVING", "AUTHORIZED", "DISPATCHED",
                        "RUNNING"):
            tasks.transition(conn, task["task_id"], state)
        done = tasks.complete_task(conn, task["task_id"], {"ok": 1})
        assert done["status"] == "COMPLETED"
        with pytest.raises(tasks.TaskError):
            tasks.transition(conn, task["task_id"], "RUNNING")
        with pytest.raises(tasks.TaskError):
            tasks.retry_task(conn, task["task_id"])
    finally:
        conn.close()


def test_fail_records_reason(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = _make(conn)
        failed = tasks.fail_task(conn, task["task_id"], "TIMEOUT", "slow")
        assert failed["status"] == "FAILED"
        assert failed["error_code"] == "TIMEOUT"
        assert "slow" in failed["error_detail"]
    finally:
        conn.close()


def test_idempotency_same_and_conflict(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        first = tasks.create_task(
            conn, target_agent="r", capability="c",
            idempotency_key="k1")
        again = tasks.create_task(
            conn, target_agent="r", capability="c",
            idempotency_key="k1")
        assert again["task_id"] == first["task_id"]
        with pytest.raises(tasks.TaskError) as exc:
            tasks.create_task(
                conn, target_agent="other", capability="c",
                idempotency_key="k1")
        assert exc.value.code == "IDEMPOTENCY_CONFLICT"
        third = tasks.create_task(
            conn, target_agent="r", capability="c",
            idempotency_key="k2")
        assert third["task_id"] != first["task_id"]
    finally:
        conn.close()


def test_cancel_propagates_to_children(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        parent = tasks.create_task(
            conn, task_input={"group": True})
        child = tasks.create_task(
            conn, target_agent="r", capability="c",
            parent_task_id=parent["task_id"])
        out = tasks.cancel_task(conn, parent["task_id"])
        assert out["status"] == "CANCELLED"
        assert tasks.get_task(conn, child["task_id"])["status"] == "CANCELLED"
        kids = tasks.task_children(conn, parent["task_id"])
        assert [k["task_id"] for k in kids] == [child["task_id"]]
    finally:
        conn.close()


def test_cancel_terminal_rejected(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = _make(conn)
        tasks.cancel_task(conn, task["task_id"])
        with pytest.raises(tasks.TaskError):
            tasks.cancel_task(conn, task["task_id"])
    finally:
        conn.close()


def test_retry_lineage_and_limits(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = _make(conn)
        tasks.fail_task(conn, task["task_id"], "TIMEOUT", "slow")
        retry = tasks.retry_task(conn, task["task_id"])
        assert retry["retry_of"] == task["task_id"]
        assert retry["retry_count"] == 1
        assert retry["correlation_id"] == task["correlation_id"]
        assert retry["status"] == "PENDING"
        tasks.fail_task(conn, retry["task_id"], "TIMEOUT", "x")
        tasks.fail_task(
            conn,
            tasks.retry_task(conn, retry["task_id"])["task_id"],
            "TIMEOUT", "x")
        third = tasks.retry_task(
            conn,
            tasks.list_tasks(conn, status="FAILED")[0]["task_id"])
        tasks.fail_task(conn, third["task_id"], "TIMEOUT", "x")
        with pytest.raises(tasks.TaskError) as exc:
            tasks.retry_task(conn, third["task_id"])
        assert exc.value.code == "RETRY_EXHAUSTED"
    finally:
        conn.close()


def test_retry_refuses_permanent_denials(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = _make(conn)
        tasks.fail_task(conn, task["task_id"], "POLICY_DENIED", "no")
        with pytest.raises(tasks.TaskError) as exc:
            tasks.retry_task(conn, task["task_id"])
        assert exc.value.code == "NOT_RETRYABLE"
        with pytest.raises(tasks.TaskError):
            tasks.retry_task(conn, tasks.create_task(
                conn, target_agent="r")["task_id"])
    finally:
        conn.close()


def test_sweep_timeouts(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = _make(conn)
        tasks.transition(conn, task["task_id"], "RESOLVING")
        tasks.transition(conn, task["task_id"], "AUTHORIZED")
        tasks.transition(conn, task["task_id"], "DISPATCHED")
        conn.execute(
            "UPDATE tasks SET deadline_at = '2000-01-01T00:00:00Z'"
            " WHERE task_id = ?", (task["task_id"],))
        conn.commit()
        assert tasks.sweep_timeouts(conn) == [task["task_id"]]
        assert tasks.get_task(conn, task["task_id"])["status"] == "FAILED"
        assert tasks.sweep_timeouts(conn) == []
    finally:
        conn.close()


def test_actor_access_rules(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        task = tasks.create_task(
            conn, requesting_agent_id="mgr-id",
            target_agent="res-id")
        assert tasks.require_task_actor(
            conn, task["task_id"], "owner")["task_id"] == task["task_id"]
        assert tasks.require_task_actor(
            conn, task["task_id"], "mgr-id")["task_id"] == task["task_id"]
        assert tasks.require_task_actor(
            conn, task["task_id"], "res-id")["task_id"] == task["task_id"]
        with pytest.raises(tasks.TaskError) as exc:
            tasks.require_task_actor(conn, task["task_id"], "stranger")
        assert exc.value.code == "FORBIDDEN"
    finally:
        conn.close()


def test_close_parent_combines_results(db_env):
    from app import tasks

    conn = _conn(db_env)
    try:
        parent = tasks.create_task(conn, task_input={"group": True})
        c1 = tasks.create_task(
            conn, target_agent="r", parent_task_id=parent["task_id"])
        c2 = tasks.create_task(
            conn, target_agent="r", parent_task_id=parent["task_id"])
        assert tasks.close_parent_if_done(conn, parent["task_id"]) is None
        tasks.complete_task(conn, c1["task_id"], {"a": 1})
        assert tasks.close_parent_if_done(conn, parent["task_id"]) is None
        tasks.fail_task(conn, c2["task_id"], "TIMEOUT", "x")
        closed = tasks.close_parent_if_done(conn, parent["task_id"])
        assert closed["status"] == "FAILED"
        assert closed["error_code"] == "CHILD_FAILED"
    finally:
        conn.close()
