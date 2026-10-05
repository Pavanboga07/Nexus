"""Provider + model configuration (V8): UI-settable base URL and model.

Gemini (and any OpenAI-compatible provider) needs more than a key —
the base URL and model must travel with it. These prove the stored
> env > default resolution, the settings endpoints, and the model
listing used by the UI picker.
"""

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


def test_base_url_model_defaults(isolated_db):
    from app import machine_config

    assert machine_config.get_llm_base_url() is None
    assert machine_config.get_llm_model() is None
    assert (
        machine_config.resolve_llm_base_url() == "https://api.openai.com/v1"
    )
    assert machine_config.resolve_llm_model() == "gpt-4o-mini"


def test_base_url_model_round_trip(isolated_db):
    from app import machine_config

    machine_config.set_llm_base_url("https://example.com/v1/")
    machine_config.set_llm_model("my-model")
    assert machine_config.get_llm_base_url() == "https://example.com/v1"
    assert machine_config.get_llm_model() == "my-model"
    assert machine_config.resolve_llm_base_url() == "https://example.com/v1"
    assert machine_config.resolve_llm_model() == "my-model"
    with pytest.raises(ValueError):
        machine_config.set_llm_base_url("   ")
    with pytest.raises(ValueError):
        machine_config.set_llm_model("")


def test_env_fallback_when_nothing_stored(isolated_db, monkeypatch):
    from app import machine_config

    monkeypatch.setenv("NEXUS_LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("NEXUS_LLM_MODEL", "env-model")
    assert machine_config.resolve_llm_base_url() == "https://env.example/v1"
    assert machine_config.resolve_llm_model() == "env-model"


def test_provider_config_honors_stored_values(isolated_db):
    from app import machine_config
    from app.llm.provider import get_llm_config

    machine_config.set_llm_key("k")
    machine_config.set_llm_base_url("https://example.com/v1")
    machine_config.set_llm_model("my-model")
    config = get_llm_config()
    assert (config.api_key, config.base_url, config.model) == (
        "k",
        "https://example.com/v1",
        "my-model",
    )


def test_settings_key_save_stores_provider_and_model(isolated_db, monkeypatch):
    import app.api.routes.settings as settings_route
    from app.main import app

    seen: dict = {}

    def _ok(key, base_url, timeout=10.0):
        seen["key"] = key
        seen["base_url"] = base_url

    monkeypatch.setattr(settings_route, "verify_llm_key", _ok)
    client = TestClient(app, headers=auth_headers())
    resp = client.post(
        "/settings/llm-key",
        json={
            "key": "gem-key",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-2.5-flash",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert seen["base_url"] == (
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )

    from app import machine_config

    assert machine_config.get_llm_key() == "gem-key"
    assert (
        machine_config.resolve_llm_base_url()
        == "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    assert machine_config.resolve_llm_model() == "gemini-2.5-flash"
    status = client.get("/settings/llm-status").json()
    assert status["base_url"].startswith("https://generativelanguage")
    assert status["model"] == "gemini-2.5-flash"


def test_settings_provider_switch_without_key(isolated_db, monkeypatch):
    import app.api.routes.settings as settings_route
    from app.main import app

    def _boom(key, base_url, timeout=10.0):
        raise AssertionError("no verification without a key")

    monkeypatch.setattr(settings_route, "verify_llm_key", _boom)
    client = TestClient(app, headers=auth_headers())
    resp = client.post(
        "/settings/llm-key",
        json={"base_url": "https://example.com/v1", "model": "m"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    from app import machine_config

    assert machine_config.get_llm_key() is None
    assert machine_config.resolve_llm_base_url() == "https://example.com/v1"
    assert machine_config.resolve_llm_model() == "m"


def test_settings_models_endpoint_lists_ids(isolated_db, monkeypatch):
    import httpx

    from app.main import app

    from app import machine_config

    machine_config.set_llm_key("k")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "b"}, {"id": "a"}, {"id": ""}, {}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    client = TestClient(app, headers=auth_headers())
    resp = client.get("/settings/llm-models")
    assert resp.status_code == 200
    assert resp.json() == {"models": ["a", "b"]}


def test_settings_models_needs_key(isolated_db):
    from app.main import app

    client = TestClient(app, headers=auth_headers())
    resp = client.get("/settings/llm-models")
    assert resp.status_code == 400
    assert resp.json()["code"] == "NO_KEY"
