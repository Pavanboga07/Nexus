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


__all__ = [
    "MACHINE_FILENAME",
    "MachineConfigError",
    "data_dir",
    "get_llm_key",
    "get_or_create_identity_secret",
    "machine_file_path",
    "resolve_llm_key",
    "set_llm_key",
]
