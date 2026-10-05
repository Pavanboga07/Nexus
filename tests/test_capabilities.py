"""Capabilities: structured, versioned, agent-owned, card-advertised."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return db


def _client():
    from app.main import app

    return TestClient(app, headers=auth_headers())


def _agent(client, name="research"):
    resp = client.post("/agents", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_register_list_get_capability(isolated_db):
    client = _client()
    agent = _agent(client)
    created = client.post(
        f"/agents/{agent['id']}/capabilities",
        json={
            "name": "web_search",
            "version": 1,
            "description": "Search the web",
            "input_schema": {"type": "object"},
            "tool": "web_search",
        },
    ).json()
    assert created["id"] == "research.web_search@v1"
    assert created["tool"] == "web_search"
    assert created["input_schema"] == {"type": "object"}
    assert created["status"] == "active"
    listed = client.get(f"/agents/{agent['id']}/capabilities").json()
    assert [c["id"] for c in listed["capabilities"]] == [
        "research.web_search@v1"
    ]


def test_register_validates(isolated_db):
    client = _client()
    agent = _agent(client)
    base = f"/agents/{agent['id']}/capabilities"
    assert client.post(base, json={"name": "Bad Name"}).status_code == 400
    assert client.post(base, json={"name": "ok", "version": 0}).status_code == 400
    assert (
        client.post(base, json={"name": "ok", "tool": "nope"}).status_code
        == 400
    )
    # Wrong JSON type is a 422 (FastAPI request validation, not ours).
    assert (
        client.post(base, json={"name": "ok", "input_schema": [1]}).status_code
        == 422
    )
    assert client.post("/agents/nope/capabilities", json={"name": "ok"}).status_code == 404


def test_register_dedups_versions(isolated_db):
    client = _client()
    agent = _agent(client)
    base = f"/agents/{agent['id']}/capabilities"
    assert client.post(base, json={"name": "x"}).status_code == 200
    assert client.post(base, json={"name": "x"}).status_code == 409
    assert client.post(base, json={"name": "x", "version": 2}).status_code == 200


def test_disable_enable_capability(isolated_db):
    client = _client()
    agent = _agent(client)
    base = f"/agents/{agent['id']}/capabilities"
    client.post(base, json={"name": "x", "tool": "web_search"})
    cap_id = "research.x@v1"
    assert (
        client.post(f"{base}/{cap_id}/disable").json()["status"] == "disabled"
    )
    assert (
        client.post(f"{base}/{cap_id}/enable").json()["status"] == "active"
    )


def test_agent_card_signed_with_capabilities(isolated_db):
    from relay.directory import verify_card

    client = _client()
    agent = _agent(client, "coder")
    client.post(
        f"/agents/{agent['id']}/capabilities",
        json={"name": "code_review", "input_schema": {"type": "object"}},
    )
    card = client.get(f"/agents/{agent['id']}/card").json()
    assert card["agent_id"] == agent["agent_id"]
    assert card["endpoint"] == "local"
    assert card["capabilities"] == [
        {
            "id": "coder.code_review@v1",
            "version": 1,
            "input_schema": {"type": "object"},
            "output_schema": {},
        }
    ]
    verify_card(card)  # raises when invalid


def test_agent_card_disabled_capability_hidden(isolated_db):
    client = _client()
    agent = _agent(client)
    base = f"/agents/{agent['id']}/capabilities"
    client.post(base, json={"name": "x"})
    client.post(f"{base}/research.x@v1/disable")
    card = client.get(f"/agents/{agent['id']}/card").json()
    assert card["capabilities"] == []


def test_agent_card_unknown_agent_404(isolated_db):
    client = _client()
    assert client.get("/agents/nope/card").status_code == 404


def test_agent_card_unauthenticated_rejected(isolated_db):
    from app.main import app

    bare = TestClient(app)
    assert bare.get("/agents/research/card").status_code == 401
