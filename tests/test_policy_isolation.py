"""Agent-scoped policy: rules bind to agents without weakening the engine.

Global ('*') rules keep working everywhere; agent-named rules apply
only to that agent; sensitive categories stay DENY regardless; the
default stays fail-closed ASK.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "nexus.db"))
    from app.store import migrate, open_db

    conn = open_db(str(tmp_path / "nexus.db"))
    migrate(conn)
    yield conn
    conn.close()


def decide(conn, **kw):
    from app.a2a import service

    params = {
        "peer": "someone",
        "data_category": "general",
        "purpose": "answer",
        "action": "answer",
    }
    params.update(kw)
    return service.evaluate_policy(conn, **params)


def test_global_rule_applies_to_every_agent(db_conn):
    from app.a2a import service

    service.create_rule(db_conn, peer="someone")
    assert decide(db_conn, agent="alice") == "ALLOW"
    assert decide(db_conn, agent="bob") == "ALLOW"
    assert decide(db_conn) == "ALLOW"


def test_agent_rule_does_not_leak(db_conn):
    from app.a2a import service

    service.create_rule(db_conn, peer="someone", agent="alice")
    assert decide(db_conn, agent="alice") == "ALLOW"
    assert decide(db_conn, agent="bob") == "ASK"
    assert decide(db_conn) == "ASK"  # agent-less sees globals only


def test_specificity_prefers_agent_rule(db_conn):
    from app.a2a import service

    service.create_rule(db_conn, peer="someone", agent="alice")
    service.create_rule(db_conn)  # fully wild
    assert decide(db_conn, peer="other", agent="alice") == "ALLOW"
    assert decide(db_conn, peer="other", agent="bob") == "ALLOW"


def test_sensitive_category_ignores_agent_rules(db_conn):
    from app.a2a import service

    service.create_rule(
        db_conn, peer="*", data_category="*", purpose="*",
        action="*", agent="alice",
    )
    assert (
        decide(db_conn, data_category="credentials", agent="alice")
        == "DENY"
    )


def test_empty_agent_rejected(db_conn):
    from app.a2a import service
    from app.a2a.service import A2AError

    with pytest.raises(A2AError) as exc:
        service.create_rule(db_conn, agent="  ")
    assert exc.value.code == "INVALID_RULE"


def test_policy_api_round_trip_with_agent(tmp_path, monkeypatch):
    from app.main import app

    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "nexus.db"))
    client = TestClient(app, headers=auth_headers())
    created = client.post(
        "/ask/policy", json={"peer": "someone", "agent": "alice"}
    ).json()
    assert created["agent_id"] == "alice"
    rules = client.get("/ask/policy").json()["rules"]
    assert [(r["peer"], r["agent_id"]) for r in rules] == [
        ("someone", "alice")
    ]
    assert (
        client.delete(f"/ask/policy/{created['rule_id']}").json()["removed"]
        is True
    )
