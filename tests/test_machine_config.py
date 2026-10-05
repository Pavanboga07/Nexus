"""Machine-local config + key settings tests (TDD red first).

Zero-secrets goal: the container boots with no -e flags. The identity
secret self-generates into a JSON file beside the DB (0600 where
supported), and the model key is pasted in the UI and verified live
before it is stored. No new DB tables (file keeps this out of
migrations).
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from tests.conftest import auth_headers


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point NEXUS_DB_PATH at tmp and clear secret envs for isolation."""
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    return db


def test_identity_secret_auto_generates_persists_reuses(isolated_db):
    from app import machine_config

    first = machine_config.get_or_create_identity_secret()
    assert isinstance(first, str) and len(first) >= 32
    path = machine_config.machine_file_path()
    assert path.exists()
    second = machine_config.get_or_create_identity_secret()
    assert second == first


def test_identity_env_override_wins(isolated_db, monkeypatch):
    from app import machine_config

    file_secret = machine_config.get_or_create_identity_secret()
    monkeypatch.setenv("NEXUS_IDENTITY_KEY", "explicit-env-secret")
    assert machine_config.get_or_create_identity_secret() == "explicit-env-secret"
    # File is left alone; removing the override reveals the file value.
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    assert machine_config.get_or_create_identity_secret() == file_secret


def test_llm_key_round_trip(isolated_db):
    from app import machine_config

    assert machine_config.get_llm_key() is None
    machine_config.set_llm_key("sk-test-123")
    assert machine_config.get_llm_key() == "sk-test-123"


def test_llm_key_rejects_empty(isolated_db):
    from app import machine_config

    with pytest.raises(ValueError):
        machine_config.set_llm_key("")
    with pytest.raises(ValueError):
        machine_config.set_llm_key("   ")
    assert machine_config.get_llm_key() is None


def test_provider_prefers_stored_over_env(isolated_db, monkeypatch):
    from app import machine_config
    from app.llm.provider import get_llm_config

    machine_config.set_llm_key("stored-key")
    monkeypatch.setenv("NEXUS_LLM_API_KEY", "env-key")
    assert get_llm_config().api_key == "stored-key"


def test_provider_falls_back_to_env(isolated_db, monkeypatch):
    from app.llm.provider import get_llm_config

    monkeypatch.setenv("NEXUS_LLM_API_KEY", "env-key")
    assert get_llm_config().api_key == "env-key"


def test_provider_missing_key(isolated_db):
    from app.llm.provider import MissingKeyError, get_llm_config

    with pytest.raises(MissingKeyError, match="MISSING_KEY"):
        get_llm_config()


def test_corrupt_machine_file_clear_error(isolated_db):
    from app import machine_config

    path = machine_config.machine_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(machine_config.MachineConfigError, match="corrupt"):
        machine_config.get_or_create_identity_secret()


def test_settings_bad_key_rejected_without_storing(isolated_db, monkeypatch):
    import app.api.routes.settings as settings_route
    from app.main import app

    def _boom(key, base_url, timeout=10.0):
        raise settings_route.LlmKeyVerificationError(
            "provider says no: INVALID_KEY (HTTP 401): bad key"
        )

    monkeypatch.setattr(settings_route, "verify_llm_key", _boom)
    client = TestClient(app, headers=auth_headers())
    resp = client.post("/settings/llm-key", json={"key": "bad-key"})
    assert resp.status_code == 401
    assert "bad key" in resp.json()["detail"]

    from app import machine_config

    assert machine_config.get_llm_key() is None
    status = client.get("/settings/llm-status").json()
    assert status["configured"] is False


def test_settings_good_key_stored_and_status_hides_value(
    isolated_db, monkeypatch
):
    import app.api.routes.settings as settings_route
    from app.main import app

    monkeypatch.setattr(
        settings_route, "verify_llm_key", lambda key, base_url, timeout=10.0: None
    )
    client = TestClient(app, headers=auth_headers())
    resp = client.post("/settings/llm-key", json={"key": "good-key"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    from app import machine_config

    assert machine_config.get_llm_key() == "good-key"
    status = client.get("/settings/llm-status").json()
    assert status["configured"] is True
    assert "good-key" not in json.dumps(status)
    assert "provider_hint" in status


def test_settings_empty_key_rejected(isolated_db):
    from app.main import app

    client = TestClient(app, headers=auth_headers())
    resp = client.post("/settings/llm-key", json={"key": "   "})
    assert resp.status_code == 400


def test_pairing_invite_auto_initializes_without_env_secret(
    isolated_db, monkeypatch
):
    """First pairing call must init identity, not fail NO_IDENTITY.

    No relay is configured, so the invite publish fails with NO_RELAY —
    which still proves the secret auto-generated and the identity was
    initialized (the failure is past the identity step).
    """

    monkeypatch.delenv("NEXUS_RELAY_URL", raising=False)
    from app.main import app

    client = TestClient(app, headers=auth_headers())
    resp = client.post("/pairing/invites", json={})
    body = resp.json()
    assert body.get("code") != "NO_IDENTITY", body

    from app import machine_config
    from app.identity.service import load_identity
    from app.store import migrate, open_db

    secret = machine_config.get_or_create_identity_secret()
    assert secret
    conn = open_db(os.environ["NEXUS_DB_PATH"])
    migrate(conn)
    try:
        view = load_identity(conn, secret)
    finally:
        conn.close()
    assert view.agent_id.startswith("nexus:")
