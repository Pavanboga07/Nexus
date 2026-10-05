"""Machine-local file config (zero secrets at boot).

The container runs with no ``-e`` secrets: the identity secret
self-generates into a JSON file beside the SQLite DB (``0600`` where
supported), and the model key is pasted in the UI and verified live
before it is stored. No DB tables (stays out of migrations).

Precedence (documented):
- Identity secret: explicit ``NEXUS_IDENTITY_KEY`` env wins, then the
  ``identity_secret`` in the machine file (auto-created on first need
  with :func:`secrets.token_urlsafe`). The value is never logged.
- Model key: stored file key wins, then ``NEXUS_LLM_API_KEY`` env,
  else missing (``MISSING_KEY``). A newly saved key applies without a
  restart (the provider resolves per turn, no cache).

A corrupted machine file raises :class:`MachineConfigError` with a
clear message (identity callers surface ``NO_IDENTITY`` for it).
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

MACHINE_FILENAME = "machine.json"

#: Separate operator-token file (NOT inside machine.json) so the token
#: can be revealed (`cat` the file) without exposing the model key or
#: the identity secret that share the machine document.
OPERATOR_TOKEN_FILENAME = "operator_token"


class MachineConfigError(RuntimeError):
    """Machine file missing, unreadable, or corrupted."""


def data_dir() -> Path:
    """Directory holding the DB (and the machine file next to it)."""
    raw = os.environ.get("NEXUS_DB_PATH", "").strip() or "data/nexus.db"
    parent = os.path.dirname(raw)
    return Path(parent) if parent else Path("data")


def machine_file_path() -> Path:
    return data_dir() / MACHINE_FILENAME


def _read_doc() -> dict[str, Any]:
    path = machine_file_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MachineConfigError(
            f"Machine config is corrupted ({path}): {exc}. "
            "Fix or delete the file to regenerate (deleting resets "
            "the local identity and model key)."
        ) from exc
    if not isinstance(data, dict):
        raise MachineConfigError(
            f"Machine config is corrupted ({path}): expected a JSON object. "
            "Fix or delete the file to regenerate (deleting resets "
            "the local identity and model key)."
        )
    return data


def _write_doc(doc: dict[str, Any]) -> None:
    path = machine_file_path()
    parent = path.parent
    if str(parent) not in ("", "."):
        os.makedirs(parent, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass  # 0600 best-effort (e.g. Windows ACLs)


def get_or_create_identity_secret() -> str:
    """Machine-local identity secret: env wins, then file (auto-create)."""
    env = os.environ.get("NEXUS_IDENTITY_KEY", "").strip()
    if env:
        return env
    doc = _read_doc()
    existing = doc.get("identity_secret", "")
    if isinstance(existing, str) and existing.strip():
        return existing
    fresh = secrets.token_urlsafe(32)
    doc["identity_secret"] = fresh
    _write_doc(doc)
    return fresh


def operator_token_path() -> Path:
    return data_dir() / OPERATOR_TOKEN_FILENAME


def _write_secret_file(path: Path, value: str) -> None:
    parent = path.parent
    if str(parent) not in ("", "."):
        os.makedirs(parent, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass  # 0600 best-effort (e.g. Windows ACLs)


def get_or_create_operator_token() -> tuple[str, bool]:
    """Operator API token: ``NEXUS_OPERATOR_TOKEN`` env wins, else a file
    token auto-created on first need. Returns ``(token, created_now)``;
    the value must never be logged — callers may log the file path only.
    """
    env = os.environ.get("NEXUS_OPERATOR_TOKEN", "").strip()
    if env:
        return env, False
    path = operator_token_path()
    try:
        stored = path.read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    if stored:
        return stored, False
    fresh = secrets.token_urlsafe(32)
    _write_secret_file(path, fresh)
    return fresh, True


def rotate_operator_token() -> str:
    """Replace the file operator token (env-pinned tokens are NOT rotated
    — unset ``NEXUS_OPERATOR_TOKEN`` first). Returns the new token once;
    the caller is responsible for delivering it to the operator."""
    if os.environ.get("NEXUS_OPERATOR_TOKEN", "").strip():
        raise MachineConfigError(
            "Operator token is pinned by NEXUS_OPERATOR_TOKEN; "
            "unset it before rotating the file token."
        )
    fresh = secrets.token_urlsafe(32)
    _write_secret_file(operator_token_path(), fresh)
    return fresh


def verify_operator_token(candidate: str) -> bool:
    """Constant-time check of a presented bearer token. Never raises."""
    import hmac

    try:
        expected, _ = get_or_create_operator_token()
    except (OSError, MachineConfigError):
        return False
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def get_llm_key() -> str | None:
    """Stored (file) model key, or None when not set via the UI."""
    stored = _read_doc().get("llm_key", "")
    if isinstance(stored, str) and stored.strip():
        return stored
    return None


def set_llm_key(key: str) -> None:
    """Persist a verified model key. Rejects empty values."""
    cleaned = key.strip() if isinstance(key, str) else ""
    if not cleaned:
        raise ValueError("Model key must not be empty.")
    doc = _read_doc()
    doc["llm_key"] = cleaned
    _write_doc(doc)


def resolve_llm_key() -> str | None:
    """Model key resolution order: stored file key, then env, else None."""
    stored = get_llm_key()
    if stored:
        return stored
    env = os.environ.get("NEXUS_LLM_API_KEY", "").strip()
    return env or None


#: Built-in default when neither the file nor the env names a provider.
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"


def get_llm_base_url() -> str | None:
    """Stored (file) provider base URL, or None when not set via the UI."""
    stored = _read_doc().get("llm_base_url", "")
    if isinstance(stored, str) and stored.strip():
        return stored.strip().rstrip("/")
    return None


def set_llm_base_url(base_url: str) -> None:
    """Persist a provider base URL. Rejects empty values."""
    cleaned = base_url.strip().rstrip("/") if isinstance(base_url, str) else ""
    if not cleaned:
        raise ValueError("Provider base URL must not be empty.")
    doc = _read_doc()
    doc["llm_base_url"] = cleaned
    _write_doc(doc)


def resolve_llm_base_url() -> str:
    """Base URL resolution order: stored file value, then env, else default."""
    stored = get_llm_base_url()
    if stored:
        return stored
    env = os.environ.get("NEXUS_LLM_BASE_URL", "").strip()
    return env.rstrip("/") or DEFAULT_LLM_BASE_URL


def get_llm_model() -> str | None:
    """Stored (file) model id, or None when not set via the UI."""
    stored = _read_doc().get("llm_model", "")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return None


def set_llm_model(model: str) -> None:
    """Persist a model id. Rejects empty values."""
    cleaned = model.strip() if isinstance(model, str) else ""
    if not cleaned:
        raise ValueError("Model must not be empty.")
    doc = _read_doc()
    doc["llm_model"] = cleaned
    _write_doc(doc)


def resolve_llm_model() -> str:
    """Model resolution order: stored file value, then env, else default."""
    stored = get_llm_model()
    if stored:
        return stored
    env = os.environ.get("NEXUS_LLM_MODEL", "").strip()
    return env or DEFAULT_LLM_MODEL


__all__ = [
    "DEFAULT_LLM_BASE_URL",
    "DEFAULT_LLM_MODEL",
    "MACHINE_FILENAME",
    "MachineConfigError",
    "data_dir",
    "get_llm_base_url",
    "get_llm_key",
    "get_llm_model",
    "get_or_create_identity_secret",
    "machine_file_path",
    "get_or_create_operator_token",
    "operator_token_path",
    "resolve_llm_base_url",
    "resolve_llm_key",
    "resolve_llm_model",
    "rotate_operator_token",
    "set_llm_base_url",
    "set_llm_key",
    "set_llm_model",
    "verify_operator_token",
]
