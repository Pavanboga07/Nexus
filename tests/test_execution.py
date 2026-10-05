"""Execution gate: authorization strictly before tools.

Self execution is owner authority; anyone else presents a live grant.
Policy (DENY > ASK > ALLOW) and approval bindings are enforced before
the tool runs — the fake executor counts invocations to prove it.
"""

from __future__ import annotations

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


SECRET = "execution-test-secret-xxxxxxxxxxxx"
CALLS: list = []


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


async def _fake_execute(tools, name, args):
    CALLS.append((name, dict(args)))
    return json.dumps({"ok": True, "echo": args})


@pytest.fixture
def world(db_env, monkeypatch):
    from app import agents, capabilities
    from app.a2a import service

    monkeypatch.setattr("app.agent.tools.execute_tool", _fake_execute)
    CALLS.clear()
    conn = _conn(db_env)
    manager = agents.create_agent(conn, SECRET, "manager")
    research = agents.create_agent(conn, SECRET, "research")
    cap = capabilities.register_capability(
        conn, "research", "web_search", tool="web_search"
    )
    service.create_rule(conn, peer="*", action="*")
    yield {
        "conn": conn,
        "manager": manager,
        "research": research,
        "cap": cap,
    }
    conn.close()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _execute(conn, **kw):
    from app.execution import execute_capability

    params = {
        "acting_ref": "research",
        "capability_id": "research.web_search@v1",
        "args": {"query": "x"},
        "purpose": "answer",
    }
    params.update(kw)
    return _run(execute_capability(conn, **params))


def _grant(conn, **kw):
    from app import delegations

    params = {
        "task_id": "corr_exec001",
        "ttl_seconds": 3600,
    }
    params.update(kw)
    return delegations.issue_grant(
        conn, SECRET, "manager",
        params.pop("recipient"), params.pop("capability"),
        params.pop("purpose", "answer"), **params,
    )


def test_self_execution_runs_tool(world):
    out = _execute(world["conn"])
    assert out["tool"] == "web_search"
    assert out["result"] == {"ok": True, "echo": {"query": "x"}}
    assert CALLS and CALLS[0][0] == "web_search"


def test_self_execution_ask_needs_approval(world):
    from app.a2a import service
    from app.execution import ExecutionError

    conn = world["conn"]
    conn.execute("DELETE FROM policy_rules")
    conn.commit()
    with pytest.raises(ExecutionError) as exc:
        _execute(conn, task_id="corr_noask")
    assert exc.value.code == "APPROVAL_REQUIRED"
    assert CALLS == []


def test_self_execution_sensitive_denied(world):
    from app.execution import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        _execute(
            world["conn"],
            args={"query": "x", "data_category": "credentials"},
        )
    assert exc.value.code == "POLICY_DENY"
    assert CALLS == []


def test_cross_agent_without_grant_rejected(world):
    from app.execution import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        _execute(world["conn"], requester_ref="manager")
    assert exc.value.code == "NO_DELEGATION"
    assert CALLS == []


def test_cross_agent_with_grant_executes(world):
    from app import delegations

    conn = world["conn"]
    grant = _grant(
        conn, recipient=world["research"]["agent_id"],
        capability="research.web_search@v1",
    )
    out = _execute(
        conn, requester_ref="manager", delegation_id=grant["id"],
        task_id="corr_exec001",
    )
    assert out["tool"] == "web_search"
    events = conn.execute(
        "SELECT action FROM delegation_events WHERE delegation_id = ?",
        (grant["id"],)).fetchall()
    assert "used" in [r[0] for r in events]


def test_cross_agent_expired_grant_denied(world):
    import base64

    from app import agents, delegations
    from app.delegations import _canonical_grant
    from app.execution import ExecutionError
    from app.identity import crypto

    conn = world["conn"]
    grant = _grant(
        conn, recipient=world["research"]["agent_id"],
        capability="research.web_search@v1",
    )
    # Age the grant honestly: rewrite expiry AND re-sign with the
    # issuer key, so only expiry (not integrity) is under test.
    priv, _ = agents.resolve_signing_key(conn, SECRET, "manager")
    aged = dict(delegations.get_grant(conn, grant["id"]))
    aged["expires_at"] = "2000-01-01T00:00:00Z"
    aged["signature"] = base64.b64encode(
        crypto.sign_bytes(priv, _canonical_grant(aged))
    ).decode("ascii")
    conn.execute(
        "UPDATE delegations SET expires_at = ?, signature = ?"
        " WHERE id = ?",
        (aged["expires_at"], aged["signature"], grant["id"]),
    )
    conn.commit()
    with pytest.raises(ExecutionError) as exc:
        _execute(conn, requester_ref="manager", delegation_id=grant["id"],
                 task_id="corr_exec001")
    assert exc.value.code == "DELEGATION_DENIED"
    assert CALLS == []


def test_grant_requiring_approval_needs_decided_row(world):
    from app import delegations
    from app.execution import ExecutionError

    conn = world["conn"]
    grant = _grant(
        conn, recipient=world["research"]["agent_id"],
        capability="research.web_search@v1",
        constraints={"require_approval": True},
    )
    with pytest.raises(ExecutionError) as exc:
        _execute(conn, requester_ref="manager", delegation_id=grant["id"],
                 task_id="corr_exec001")
    assert exc.value.code == "APPROVAL_REQUIRED"
    conn.execute(
        "INSERT INTO a2a_approvals (approval_id, message_id,"
        " correlation_id, requester, action, data_category, purpose,"
        " question, expires_at, status, decided_at, created_at)"
        " VALUES ('apr_e1', 'msg_e1', 'corr_exec001', 'x', 'answer',"
        " 'general', 'answer', 'q', '2030-01-01T00:00:00Z',"
        " 'approved', '', '')"
    )
    conn.commit()
    out = _execute(conn, requester_ref="manager",
                   delegation_id=grant["id"], task_id="corr_exec001")
    assert out["tool"] == "web_search"


def test_disabled_acting_agent_rejected(world):
    from app import agents
    from app.execution import ExecutionError

    conn = world["conn"]
    agents.set_agent_status(conn, "research", "disabled")
    with pytest.raises(ExecutionError) as exc:
        _execute(conn)
    assert exc.value.code == "AGENT_INACTIVE"
    assert CALLS == []


def test_foreign_capability_rejected(world):
    from app import capabilities
    from app.execution import ExecutionError

    conn = world["conn"]
    capabilities.register_capability(conn, "manager", "other",
                                     tool="web_search")
    with pytest.raises(ExecutionError) as exc:
        _execute(conn, capability_id="manager.other@v1")
    assert exc.value.code == "WRONG_TOOL_OWNER"
    assert CALLS == []


def test_untooled_capability_rejected(world):
    from app import capabilities
    from app.execution import ExecutionError

    conn = world["conn"]
    capabilities.register_capability(conn, "research", "advisory")
    with pytest.raises(ExecutionError) as exc:
        _execute(conn, capability_id="research.advisory@v1")
    assert exc.value.code == "NO_TOOL"
    assert CALLS == []


def test_unknown_capability_rejected(world):
    from app.execution import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        _execute(world["conn"], capability_id="research.nope@v9")
    assert exc.value.code == "NOT_FOUND"
    assert CALLS == []


def test_max_uses_exhausted_denied(world):
    from app import delegations
    from app.execution import ExecutionError

    conn = world["conn"]
    grant = _grant(
        conn, recipient=world["research"]["agent_id"],
        capability="research.web_search@v1",
        constraints={"max_uses": 1},
    )
    _execute(conn, requester_ref="manager", delegation_id=grant["id"],
             task_id="corr_exec001")
    with pytest.raises(ExecutionError) as exc:
        _execute(conn, requester_ref="manager", delegation_id=grant["id"],
                 task_id="corr_exec001")
    assert exc.value.code == "DELEGATION_DENIED"
    assert len(CALLS) == 1
