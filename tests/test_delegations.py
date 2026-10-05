"""Delegation grants: issuance, verification, evaluation, revocation."""

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


SECRET = "delegation-test-secret-xxxxxxxxxxxx"


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


def _two_agents(conn):
    from app import agents

    a = agents.create_agent(conn, SECRET, "manager")
    b = agents.create_agent(conn, SECRET, "research")
    return a, b


def _cap(conn, handle, name, tool=None):
    from app import capabilities

    return capabilities.register_capability(
        conn, handle, name, tool=tool,
        input_schema={"type": "object"},
    )


def _grant(conn, issuer="manager", recipient=None, cap=None, **kw):
    from app import delegations

    recipient = recipient or "RECIPIENT"
    params = {
        "purpose": "answer",
        "task_id": "corr_task001",
        "ttl_seconds": 3600,
    }
    params.update(kw)
    return delegations.issue_grant(
        conn, SECRET, issuer, recipient, cap,
        params.pop("purpose"), **params,
    )


def test_issue_valid_grant(db_env):
    from app import delegations

    conn = _conn(db_env)
    try:
        a, b = _two_agents(conn)
        cap = _cap(conn, "research", "web_search", tool="web_search")
        grant = _grant(conn, recipient=b["agent_id"], cap=cap["id"])
        assert grant["issuer_agent_id"] == a["agent_id"]
        assert grant["capability_id"] == cap["id"]
        assert grant["status"] == "active"
        assert grant["signature"]
        delegations.verify_grant_signature(conn, {**grant,
            "signature": conn.execute(
                "SELECT signature FROM delegations WHERE id = ?",
                (grant["id"],)).fetchone()["signature"]})
    finally:
        conn.close()


def test_issue_validates(db_env):
    from app import delegations
    from app.delegations import DelegationError

    conn = _conn(db_env)
    try:
        a, b = _two_agents(conn)
        cap = _cap(conn, "research", "web_search")
        with pytest.raises(DelegationError) as exc:
            _grant(conn, recipient=b["agent_id"], cap=cap["id"], purpose="  ")
        assert exc.value.code == "BAD_PURPOSE"
        with pytest.raises(DelegationError) as exc:
            _grant(conn, recipient="nobody", cap=cap["id"])
        assert exc.value.code == "UNKNOWN_RECIPIENT"
        with pytest.raises(DelegationError) as exc:
            _grant(conn, recipient=b["agent_id"], cap=cap["id"],
                   ttl_seconds=0)
        assert exc.value.code == "BAD_TTL"
        other = _cap(conn, "manager", "other")
        with pytest.raises(DelegationError) as exc:
            _grant(conn, recipient=b["agent_id"], cap=other["id"])
        assert exc.value.code == "WRONG_CAPABILITY_OWNER"
    finally:
        conn.close()


def test_issue_disabled_issuer_or_capability(db_env):
    from app import agents
    from app.delegations import DelegationError

    conn = _conn(db_env)
    try:
        a, b = _two_agents(conn)
        cap = _cap(conn, "research", "web_search")
        agents.set_agent_status(conn, "manager", "disabled")
        with pytest.raises(DelegationError) as exc:
            _grant(conn, recipient=b["agent_id"], cap=cap["id"])
        assert exc.value.code == "ISSUER_INACTIVE"
        agents.set_agent_status(conn, "manager", "active")
        from app import capabilities

        capabilities.set_capability_status(conn, cap["id"], "disabled")
        with pytest.raises(DelegationError) as exc:
            _grant(conn, recipient=b["agent_id"], cap=cap["id"])
        assert exc.value.code == "CAPABILITY_DISABLED"
    finally:
        conn.close()


def test_tampered_grant_rejected(db_env):
    from app import delegations
    from app.delegations import DelegationError

    conn = _conn(db_env)
    try:
        a, b = _two_agents(conn)
        cap = _cap(conn, "research", "web_search")
        grant = _grant(conn, recipient=b["agent_id"], cap=cap["id"])
        row = conn.execute(
            "SELECT signature FROM delegations WHERE id = ?",
            (grant["id"],)).fetchone()
        forged = {**grant, "signature": row["signature"],
                  "purpose": "evil"}
        with pytest.raises(DelegationError) as exc:
            delegations.verify_grant_signature(conn, forged)
        assert exc.value.code == "BAD_SIGNATURE"
        with pytest.raises(DelegationError):
            delegations.verify_grant_signature(conn, {**grant,
                                                      "signature": ""})
    finally:
        conn.close()


def test_evaluate_covers_all_mismatches(db_env):
    from app import delegations

    conn = _conn(db_env)
    try:
        a, b = _two_agents(conn)
        cap = _cap(conn, "research", "web_search")
        grant = _grant(conn, recipient=b["agent_id"], cap=cap["id"])
        good = dict(
            recipient_agent_id=b["agent_id"],
            capability_id=cap["id"], purpose="answer",
            task_id="corr_task001",
        )
        assert delegations.evaluate_grant(conn, grant, **good) == (True, "ok")
        cases = [
            (dict(good, recipient_agent_id="nobody"),
             "delegation names a different recipient"),
            (dict(good, capability_id="x"),
             "delegation names a different capability"),
            (dict(good, purpose="spy"),
             "delegation names a different purpose"),
            (dict(good, task_id="other"),
             "delegation is bound to a different task"),
        ]
        for kwargs, expected in cases:
            ok, why = delegations.evaluate_grant(conn, grant, **kwargs)
            assert (ok, why) == (False, expected), kwargs
    finally:
        conn.close()


def test_evaluate_expiry_revocation_budget(db_env):
    from app import delegations

    conn = _conn(db_env)
    try:
        a, b = _two_agents(conn)
        cap = _cap(conn, "research", "web_search")
        base = dict(recipient_agent_id=b["agent_id"],
                    capability_id=cap["id"], purpose="answer",
                    task_id="corr_task001")
        limited = _grant(conn, recipient=b["agent_id"], cap=cap["id"],
                         constraints={"max_uses": 1})
        assert delegations.evaluate_grant(conn, limited, **base)[0] is True
        conn.execute(
            "INSERT INTO delegation_events (event_id, delegation_id,"
            " action, detail, created_at) VALUES ('dev_x1', ?, 'used',"
            " '', '')",
            (limited["id"],),
        )
        conn.commit()
        ok, why = delegations.evaluate_grant(conn, limited, **base)
        assert (ok, why) == (False, "delegation use budget exhausted")

        stale = dict(limited)
        stale["expires_at"] = "2000-01-01T00:00:00Z"
        assert delegations.evaluate_grant(conn, stale, **base) == (
            False, "delegation expired")
        delegations.revoke_grant(conn, limited["id"])
        assert delegations.evaluate_grant(
            conn, delegations.get_grant(conn, limited["id"]), **base
        ) == (False, "delegation revoked")

        ghost = dict(limited, issuer_agent_id="nexus:ed25519:00000000000000000000000000000000")
        ok, _ = delegations.evaluate_grant(conn, ghost, **base)
        assert ok is False
    finally:
        conn.close()


def test_approval_binding_requires_decided_approve(db_env):
    from app import delegations

    conn = _conn(db_env)
    try:
        a, b = _two_agents(conn)
        cap = _cap(conn, "research", "web_search")
        grant = _grant(
            conn, recipient=b["agent_id"], cap=cap["id"],
            constraints={"require_approval": True},
        )
        ok, why = delegations.check_approval_binding(
            conn, grant, "corr_task001")
        assert (ok, why) == (False, "owner approval required for this task")
        conn.execute(
            "INSERT INTO a2a_approvals (approval_id, message_id,"
            " correlation_id, requester, action, data_category, purpose,"
            " question, expires_at, status, decided_at, created_at)"
            " VALUES ('apr_t1', 'msg_t1', 'corr_task001', 'x', 'answer',"
            " 'general', 'answer', 'q', '2030-01-01T00:00:00Z',"
            " 'approved', '', '')"
        )
        conn.commit()
        assert delegations.check_approval_binding(
            conn, grant, "corr_task001") == (True, "ok")
        plain = _grant(conn, recipient=b["agent_id"], cap=cap["id"])
        assert delegations.check_approval_binding(
            conn, plain, "corr_task001") == (True, "no approval required")
    finally:
        conn.close()


def test_receive_grant_verifies_and_idempotent(db_env):
    from app import delegations
    from app.delegations import DelegationError

    conn = _conn(db_env)
    try:
        a, b = _two_agents(conn)
        cap = _cap(conn, "research", "web_search")
        grant = _grant(conn, recipient=b["agent_id"], cap=cap["id"])
        row = conn.execute(
            "SELECT signature FROM delegations WHERE id = ?",
            (grant["id"],)).fetchone()
        wire = {**grant, "signature": row["signature"]}
        conn.execute("DELETE FROM delegations WHERE id = ?", (grant["id"],))
        conn.execute(
            "DELETE FROM delegation_events WHERE delegation_id = ?",
            (grant["id"],))
        conn.commit()
        stored = delegations.receive_grant(conn, wire)
        assert stored["id"] == grant["id"]
        again = delegations.receive_grant(conn, wire)
        assert again["id"] == grant["id"]
        with pytest.raises(DelegationError):
            delegations.receive_grant(conn, {**wire, "purpose": "evil"})
    finally:
        conn.close()


def test_revoke_unknown_grant_404(db_env):
    from app import delegations
    from app.delegations import DelegationError

    conn = _conn(db_env)
    try:
        with pytest.raises(DelegationError) as exc:
            delegations.revoke_grant(conn, "dlg_nope")
        assert exc.value.code == "NOT_FOUND"
    finally:
        conn.close()


def test_delegation_http_round_trip(db_env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import auth_headers

    monkeypatch.setenv(
        "NEXUS_OPERATOR_TOKEN", "delegation-http-token")
    client = TestClient(
        app, headers={"Authorization": "Bearer delegation-http-token"})
    ra = client.post("/agents", json={"name": "mgr"}).json()
    rb = client.post("/agents", json={"name": "res"}).json()
    cap = client.post(
        f"/agents/{rb['id']}/capabilities", json={"name": "web_search"}
    ).json()
    grant = client.post(
        "/delegations/issue",
        json={
            "issuer_ref": "mgr",
            "recipient_agent_id": rb["agent_id"],
            "capability_id": cap["id"],
            "purpose": "answer",
            "task_id": "corr_http001",
        },
    ).json()
    assert grant["capability_id"] == cap["id"]
    fetched = client.get(f"/delegations/{grant['id']}").json()
    assert fetched["status"] == "active"
    assert client.post(f"/delegations/{grant['id']}/revoke").json() == {
        "id": grant["id"], "revoked": True}
    assert client.get("/delegations/nope").status_code == 404
