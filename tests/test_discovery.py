"""Discovery: structured resolution, candidates on ambiguity, never guessing."""

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


def _agent(client, name, display=None):
    resp = client.post(
        "/agents",
        json={"name": name, "display_name": display or name.title()},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cap(client, handle, name):
    resp = client.post(
        f"/agents/{handle}/capabilities", json={"name": name}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ask(client, q, fuzzy=False):
    params = {"q": q}
    if fuzzy:
        params["fuzzy"] = "true"
    return client.get("/agents/discover", params=params).json()


def test_exact_agent_id_wins(isolated_db):
    client = _client()
    agent = _agent(client, "research")
    out = _ask(client, agent["agent_id"])
    assert out["explicit"] is True
    assert [m["id"] for m in out["matches"]] == [agent["agent_id"]]
    assert out["matches"][0]["verified"] is True


def test_exact_capability_resolves(isolated_db):
    client = _client()
    agent = _agent(client, "research")
    _cap(client, "research", "web_search")
    out = _ask(client, "research.web_search@v1")
    assert out["explicit"] is True
    assert [m["id"] for m in out["matches"]] == [agent["agent_id"]]


def test_exact_handle_resolves(isolated_db):
    client = _client()
    _agent(client, "research")
    out = _ask(client, "research")
    assert out["explicit"] is True
    assert out["matches"][0]["handle"] == "research"


def test_ambiguous_capability_returns_candidates(isolated_db):
    client = _client()
    _agent(client, "research")
    _agent(client, "coding")
    _cap(client, "research", "web_search")
    _cap(client, "coding", "web_search")
    out = _ask(client, "coding.web_search@v1")
    assert out["explicit"] is True
    out2 = _ask(client, "web_search")
    assert out2["explicit"] is False
    assert out2["matches"] == []


def test_unknown_query_empty(isolated_db):
    client = _client()
    _agent(client, "research")
    assert _ask(client, "nope-nothing") == {"matches": [], "explicit": False}
    assert _ask(client, "") == {"matches": [], "explicit": False}


def test_fuzzy_opt_in_only(isolated_db):
    client = _client()
    _agent(client, "research")
    _cap(client, "research", "web_search")
    assert _ask(client, "web")["matches"] == []
    out = _ask(client, "web", fuzzy=True)
    assert out["explicit"] is False
    assert [m["handle"] for m in out["matches"]] == ["research"]


def test_display_name_match(isolated_db):
    client = _client()
    agent = _agent(client, "research", display="Deep Research")
    out = _ask(client, "Deep Research")
    assert out["explicit"] is True
    assert out["matches"][0]["id"] == agent["agent_id"]


def test_discover_requires_auth(isolated_db):
    from app.main import app

    bare = TestClient(app)
    assert bare.get("/agents/discover", params={"q": "x"}).status_code == 401
