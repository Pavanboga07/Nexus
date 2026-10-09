"""Shared relay delivery: background send with status flip.

Moved out of the ask route so orchestrated dispatches reuse the exact
same path (``app/api/routes/ask.py`` re-exports these names; existing
tests keep working untouched).

Unlike the legacy path — which always authenticates the relay
connection as the identity singleton — delivery accepts an explicit
``sender_ref`` (a local agent): the connection then authenticates as
THAT agent, so presence, acks, and routing stay agent-accurate.
``sender_ref=None`` preserves the legacy identity behavior exactly.
"""

from __future__ import annotations

import logging
import os

QUEUED_DELIVERY = {
    "mode": "queued",
    "reason": "delivering in background; refresh to confirm",
}


def relay_ws_url(base: str) -> str:
    """HTTP(S) relay base -> ``ws(s)://.../ws`` (ws(s) passthrough)."""
    base = base.strip().rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    if not base.endswith("/ws"):
        base += "/ws"
    return base


def _sender_material(conn, secret: str | None, sender_ref: str | None):
    """Resolve (private key, agent_id, public_key_b64) for delivery auth."""
    if sender_ref:
        from app import agents

        if secret is None:
            from app.machine_config import get_or_create_identity_secret

            secret = get_or_create_identity_secret()
        agent = agents.resolve_agent(conn, sender_ref)
        priv, agent_id = agents.resolve_signing_key(conn, secret, sender_ref)
        row = conn.execute(
            "SELECT public_key FROM agent_identities WHERE agent_id = ?"
            " AND status = 'active' ORDER BY version DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - resolve_signing_key guards
            raise RuntimeError("agent has no active key")
        return priv, agent_id, str(row["public_key"]), agent
    from app.a2a.service import A2AError
    from app.identity import service as identity_service

    try:
        priv, agent_id = identity_service.local_key(conn)
    except identity_service.IdentityCorruptionError as exc:
        raise A2AError("NO_IDENTITY", str(exc), status=503) from exc
    return priv, agent_id, identity_service.local_pubkey(conn), None


async def deliver_envelope(
    message_id: str,
    envelope: dict,
    db_path: str,
    *,
    sender_ref: str | None = None,
    secret: str | None = None,
) -> None:
    """Deliver one stored envelope; flip its row to relayed/failed.

    Opens a FRESH connection (never the request ``conn``) so HTTP
    requests never wait on the relay handshake. Ack -> ``relayed``;
    any exception -> ``delivery_failed`` (both readable via polling).

    No ordering guarantee: the relay may push a newer envelope over a
    live recipient socket while older unacked rows for that same
    recipient still wait in its queue (queue/live boundary). Fixing
    that needs a queue-flush before direct live delivery in
    ``relay/main.py::handle_relay_envelope`` — relay-side, out of scope
    for this app-side delivery function.
    """
    from app.a2a import relay_client
    from app.identity import crypto
    from app.store import migrate, open_db

    conn = open_db(db_path)
    migrate(conn)
    try:
        try:
            base = os.environ.get("NEXUS_RELAY_URL", "").strip()
            priv, agent_id, pubkey_b64, _ = _sender_material(
                conn, secret, sender_ref
            )
            await relay_client.deliver_one(
                relay_ws_url(base),
                envelope,
                recipient=envelope["recipient"],
                agent_id=agent_id,
                public_key_b64=pubkey_b64,
                sign_fn=lambda data: crypto.sign_bytes(priv, data),
                timeout=relay_client.CONNECT_TIMEOUT,
                ack_timeout=relay_client.ACK_TIMEOUT,
            )
            status = "relayed"
        except Exception as exc:  # noqa: BLE001 - failure is a row state
            status = "delivery_failed"
            logging.getLogger("nexus.delivery").warning(
                "background delivery %s failed: %s: %s",
                message_id,
                type(exc).__name__,
                str(exc)[:200],
            )
        with conn:
            conn.execute(
                "UPDATE a2a_messages SET status = ? WHERE message_id = ?",
                (status, message_id),
            )
    finally:
        conn.close()


def queue_delivery(background, signed: dict, db_path: str,
                   *, sender_ref: str | None = None,
                   secret: str | None = None) -> dict:
    """Return immediately; deliver in the background when configured.

    No relay URL -> ``local-only`` (nothing to send to). Otherwise the
    response is ``queued`` and delivery flips the stored row to
    ``relayed`` / ``delivery_failed`` for polling.
    """
    base = os.environ.get("NEXUS_RELAY_URL", "").strip()
    if not base:
        return {
            "mode": "local-only",
            "reason": "relay is not configured (set NEXUS_RELAY_URL).",
        }
    background.add_task(
        deliver_envelope, signed["message_id"], signed, db_path,
        sender_ref=sender_ref, secret=secret,
    )
    return dict(QUEUED_DELIVERY)


__all__ = [
    "QUEUED_DELIVERY",
    "deliver_envelope",
    "queue_delivery",
    "relay_ws_url",
]
