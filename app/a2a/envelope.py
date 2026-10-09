"""App-side v0.3 envelope helpers (V4).

The frozen canonical rules live in ``relay.envelope`` (single source of
truth: strict UTC timestamps, floats rejected, signature excluded from
the signed bytes, correlation_id required). This module is a thin
app-side facade: build unsigned dicts, sign with the local key, verify
against a pinned peer key. No canonical logic is duplicated here.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from relay import envelope as _relay

PROTOCOL = _relay.PROTOCOL
VERSION = _relay.VERSION
MESSAGE_TYPES = _relay.MESSAGE_TYPES

#: Fallback envelope TTL (seconds) when neither an explicit expiry nor
#: ``ttl_seconds`` is given. The old hardcoded 300s default is now just
#: the default of ``NEXUS_ENVELOPE_TTL_SECONDS`` (audit §4 F6).
DEFAULT_TTL_SECONDS = 300


def _default_ttl_seconds() -> int:
    """Read ``NEXUS_ENVELOPE_TTL_SECONDS``; missing/garbage/negative
    values fall back to 300."""
    try:
        value = int(
            os.environ.get("NEXUS_ENVELOPE_TTL_SECONDS", "")
            or DEFAULT_TTL_SECONDS
        )
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return value if value > 0 else DEFAULT_TTL_SECONDS


def _new_id(prefix: str, explicit: str | None = None) -> str:
    if explicit is not None:
        return explicit
    return f"{prefix}_{uuid.uuid4().hex}"


def new_envelope(
    *,
    sender: str,
    recipient: str,
    message_type: str,
    payload: dict[str, Any],
    timestamp: str | None = None,
    expires_at: str | None = None,
    message_id: str | None = None,
    correlation_id: str | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Build an unsigned envelope dict (validated, unsigned).

    Expiry precedence: an explicit ``expires_at`` wins; otherwise
    ``ttl_seconds``; otherwise ``NEXUS_ENVELOPE_TTL_SECONDS`` (default
    300). Raises the relay's validation errors on strict-timestamp or
    float-payload violations (same frozen rules).
    """
    if ttl_seconds is None:
        ttl_seconds = _default_ttl_seconds()
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive.")
    env = _relay.Envelope.model_validate(
        {
            "protocol": PROTOCOL,
            "version": VERSION,
            "message_id": _new_id("msg", message_id),
            "correlation_id": _new_id("corr", correlation_id),
            "sender": sender,
            "recipient": recipient,
            "timestamp": timestamp or _relay.utc_now_iso(),
            "expires_at": expires_at or _relay.utc_iso_in(ttl_seconds),
            "message_type": message_type,
            "payload": payload,
        }
    )
    return env.unsigned_dict()


def sign(unsigned: dict[str, Any], private_key) -> dict[str, Any]:
    """Attach an Ed25519 signature over the canonical bytes."""
    return _relay.sign_envelope(private_key, unsigned)


def verify(envelope: dict[str, Any], public_key_b64: str) -> bool:
    """Verify against a pinned peer key. Never raises."""
    return _relay.verify_envelope_signature(envelope, public_key_b64)


def unsigned_dict(envelope: dict[str, Any]) -> dict[str, Any]:
    """Envelope minus its signature (the signed bytes' source).

    Delegates to the canonical ``relay.envelope`` implementation so
    the None-handling (absent, never null) matches exactly what
    ``sign``/``verify`` cover. Do not reimplement here.
    """
    return _relay.Envelope.model_validate(envelope).unsigned_dict()


def signed_error(
    private_key,
    *,
    sender: str,
    recipient: str,
    correlation_id: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    """A signed ``error`` envelope (unknown types, denials, ...)."""
    return _relay.build_signed_error(
        private_key,
        sender=sender,
        recipient=recipient,
        correlation_id=correlation_id,
        code=code,
        message=message,
    )


__all__ = [
    "MESSAGE_TYPES",
    "PROTOCOL",
    "VERSION",
    "new_envelope",
    "sign",
    "signed_error",
    "unsigned_dict",
    "verify",
]
