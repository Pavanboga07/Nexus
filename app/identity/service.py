"""Single-laptop local identity: create-or-load with sealed storage.

The private key is generated once, AES-GCM-sealed with a machine-local
secret (e.g. ``NEXUS_IDENTITY_KEY`` from the environment or a file next
to the DB — never the DB alone), and stored in the ``identity`` table
(id = 1 singleton row). Every load decrypts and self-verifies:

  1. the private key decrypts under the configured secret,
  2. it derives exactly the stored public key,
  3. the stored agent_id is the pubkey digest (self-certifying check),
  4. a probe signature round-trips.

Any failure raises :class:`IdentityCorruptionError` — startup must abort
rather than silently mint a replacement key that would break trust.
"""

from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.identity import crypto

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_SELF_VERIFY_MESSAGE = b"nexus-identity-self-verification"


class IdentityCorruptionError(RuntimeError):
    """Stored identity missing, undecryptable, or inconsistent."""


@dataclass(frozen=True)
class LocalIdentity:
    """Public, shareable view. Never carries private material."""

    agent_id: str
    public_key: str  # base64 of the raw 32-byte Ed25519 key
    fingerprint: str


def _require_secret(secret: str) -> str:
    if not secret:
        raise IdentityCorruptionError(
            "Identity secret is not configured; set the machine-local "
            "NEXUS_IDENTITY_KEY before creating or loading the identity."
        )
    return secret


def _public_view(agent_id: str, public_raw: bytes) -> LocalIdentity:
    return LocalIdentity(
        agent_id=agent_id,
        public_key=base64.b64encode(public_raw).decode("ascii"),
        fingerprint=crypto.fingerprint_from_public_key(public_raw),
    )


def _decrypt_and_check(
    public_b64: str, sealed_b64: str, agent_id: str, secret: str
):
    """Decrypt the sealed key and verify it matches the stored public/id."""
    try:
        public_raw = base64.b64decode(public_b64.encode("ascii"))
        public_key = crypto.load_public_key(public_raw)
        private_raw = crypto.decrypt_private_key(sealed_b64, secret)
        private_key = crypto.load_private_key(private_raw)
    except (crypto.IdentityCryptoError, ValueError) as exc:
        raise IdentityCorruptionError(
            f"Stored identity is corrupted or the secret is wrong: {exc}"
        ) from exc
    if not crypto.keypair_matches(private_key, public_key):
        raise IdentityCorruptionError(
            "Stored keypair is inconsistent: the private key does not "
            "derive the stored public key."
        )
    if agent_id != crypto.agent_id_from_public_key(public_raw):
        raise IdentityCorruptionError(
            "Stored agent_id does not match the stored public key."
        )
    probe = crypto.sign_bytes(private_key, _SELF_VERIFY_MESSAGE)
    if not crypto.verify_bytes(public_key, _SELF_VERIFY_MESSAGE, probe):
        raise IdentityCorruptionError(
            "Identity self-verification failed: probe signature invalid."
        )
    return private_key, public_key, public_raw


def load_identity(conn: sqlite3.Connection, secret: str) -> LocalIdentity:
    """Strict load: decrypt + self-verify, no creation. Raises on any fault."""
    _require_secret(secret)
    row = conn.execute(
        "SELECT agent_id, public_key, encrypted_private_key FROM identity WHERE id = 1"
    ).fetchone()
    if row is None:
        raise IdentityCorruptionError("No local identity stored yet.")
    _, _, public_raw = _decrypt_and_check(
        row["public_key"], row["encrypted_private_key"], row["agent_id"], secret
    )
    return _public_view(row["agent_id"], public_raw)


def ensure_identity(conn: sqlite3.Connection, secret: str) -> LocalIdentity:
    """Create-or-load the singleton identity; idempotent across restarts."""
    _require_secret(secret)
    row = conn.execute(
        "SELECT agent_id, public_key, encrypted_private_key FROM identity WHERE id = 1"
    ).fetchone()
    if row is not None:
        _, _, public_raw = _decrypt_and_check(
            row["public_key"],
            row["encrypted_private_key"],
            row["agent_id"],
            secret,
        )
        return _public_view(row["agent_id"], public_raw)
    private_key, public_key = crypto.generate_keypair()
    public_raw = crypto.public_key_bytes(public_key)
    agent_id = crypto.agent_id_from_public_key(public_raw)
    sealed = crypto.encrypt_private_key(
        crypto.private_key_bytes(private_key), secret
    )
    with conn:
        conn.execute(
            "INSERT INTO identity "
            "(id, agent_id, public_key, encrypted_private_key, created_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (
                agent_id,
                base64.b64encode(public_raw).decode("ascii"),
                sealed,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return _public_view(agent_id, public_raw)


def sign_data(conn: sqlite3.Connection, secret: str, data: bytes) -> bytes:
    """Load the local key (verified) and sign ``data``."""
    _require_secret(secret)
    row = conn.execute(
        "SELECT agent_id, public_key, encrypted_private_key FROM identity WHERE id = 1"
    ).fetchone()
    if row is None:
        raise IdentityCorruptionError("No local identity stored yet.")
    private_key, _, _ = _decrypt_and_check(
        row["public_key"], row["encrypted_private_key"], row["agent_id"], secret
    )
    return crypto.sign_bytes(private_key, data)


__all__ = [
    "IdentityCorruptionError",
    "LocalIdentity",
    "ensure_identity",
    "load_identity",
    "local_key",
    "local_pubkey",
    "sign_data",
]


def local_key(conn: sqlite3.Connection) -> tuple["Ed25519PrivateKey", str]:
    """Local ``(private key, agent_id)``; raises IdentityCorruptionError when unusable.

    Canonical home for the key-material helpers. Route and service layers must
    import these from here — never underscore-privates from a route module.
    """
    from app.machine_config import (
        MachineConfigError,
        get_or_create_identity_secret,
    )

    try:
        secret = get_or_create_identity_secret()
    except MachineConfigError as exc:
        raise IdentityCorruptionError(str(exc)) from exc
    try:
        # ensure: the secret self-generates and the identity initializes
        # on first need; corruption survives only for corrupt stores.
        view = ensure_identity(conn, secret)
    except IdentityCorruptionError:
        raise
    row = conn.execute(
        "SELECT encrypted_private_key FROM identity WHERE id = 1"
    ).fetchone()
    if row is None:  # pragma: no cover - ensure_identity guarantees the row
        raise IdentityCorruptionError("No local identity stored yet.")
    private_raw = crypto.decrypt_private_key(row["encrypted_private_key"], secret)
    return crypto.load_private_key(private_raw), view.agent_id


def local_pubkey(conn: sqlite3.Connection) -> str:
    """Local public key (base64); empty string when no identity is stored yet."""
    row = conn.execute("SELECT public_key FROM identity WHERE id = 1").fetchone()
    return str(row["public_key"]) if row is not None else ""
