"""Remote tasks: trust-gated dispatch, cross-Nexus correlation, errors."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    db = str(tmp_path / "nexus.db")
    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.delenv("NEXUS_IDENTITY_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_LLM_MODEL", raising=False)
    return db


SECRET = "remote-task-test-secret-xxxxxxxxxx"


def _conn(db):
    from app.store import migrate, open_db

    conn = open_db(db)
    migrate(conn)
    return conn


def _world(conn):
    """Local manager + paired faraway peer advertising one capability."""
    import base64
    import tempfile

    from app import agents, pairing
    from app.identity import crypto
    from app.identity.service import ensure_identity
    from app.store import migrate, open_db
    from relay.directory import sign_card
    from relay.envelope import utc_iso_in, utc_now_iso

    manager = agents.create_agent(conn, SECRET, "manager")
    peer_db = open_db(tempfile.mktemp(suffix=".db"))
    migrate(peer_db)
    peer_ident = ensure_identity(peer_db, "peer-secret-xxxxxxxxxxxxxxxx")
    row = peer_db.execute(
        "SELECT public_key FROM identity WHERE id = 1").fetchone()
    pub = row["public_key"]
    raw = base64.b64decode(pub)
    peer_priv = crypto.load_private_key(crypto.decrypt_private_key(
        peer_db.execute("SELECT encrypted_private_key FROM identity"
                        " WHERE id = 1").fetchone()["encrypted_private_key"],
        "peer-secret-xxxxxxxxxxxxxxxx"))
    card = sign_card(peer_priv, {
        "type": "agent-card", "protocol": "nexus-a2a", "version": "0.3",
        "agent_id": peer_ident.agent_id, "display_name": "Faraway",
        "public_key": pub, "endpoint": "ws://test.invalid/ws",
        "capabilities": [{"id": "faraway.survey@v1", "version": 1}],
        "supported_purposes": [], "issued_at": utc_now_iso(),
        "expires_at": utc_iso_in(3600),
    })
    pairing.approve_peer(conn, card)
    peer_db.close()
    return manager, peer_ident, peer_priv


def _run(coro):
    return asyncio.run(coro)


def test_suspended_peer_dispatch_fails_task(db_env):
    from app import delegations, orchestration, pairing, tasks

    conn = _conn(db_env)
    try:
        manager, peer, _ = _world(conn)
        grant = delegations.issue_grant(
            conn, SECRET, "manager", peer.agent_id,
            "faraway.survey@v1", "answer", ttl_seconds=3600)
        pairing.set_peer_trust(conn, peer.agent_id, "SUSPENDED")
        task = tasks.create_task(
            conn, requesting_agent_id=manager["id"],
            target_agent=peer.agent_id,
            capability="faraway.survey@v1",
            task_input={"question": "hi"},
            delegation_id=grant["id"])
        final, envelope = _run(orchestration.run_task(
            conn, task["task_id"], secret=SECRET))
        assert final["status"] == "FAILED"
        assert final["error_code"] == "DELEGATION_INVALID"
        assert envelope is None

        # Suspend AFTER authorize: the dispatch-time trust gate fires.
        from app.a2a import service

        service.create_rule(conn, peer="*", action="*")
        pairing.set_peer_trust(conn, peer.agent_id, "TRUSTED")
        task2 = tasks.create_task(
            conn, requesting_agent_id=manager["id"],
            target_agent=peer.agent_id,
            capability="faraway.survey@v1",
            task_input={"question": "hi"},
            delegation_id=grant["id"])
        orchestration.resolve_task(conn, task2["task_id"])
        orchestration.authorize_task(conn, task2["task_id"])
        pairing.set_peer_trust(conn, peer.agent_id, "SUSPENDED")
        final2, envelope2 = _run(orchestration.dispatch_task(
            conn, task2["task_id"], secret=SECRET))
        assert final2["status"] == "FAILED"
        assert final2["error_code"] == "PEER_SUSPENDED"
        assert envelope2 is None
    finally:
        conn.close()


def test_unpaired_remote_never_sent(db_env):
    from app import agents, orchestration, tasks

    conn = _conn(db_env)
    try:
        agents.create_agent(conn, SECRET, "manager")
        created = tasks.create_task(
            conn, requesting_agent_id="manager",
            target_agent="nexus:ed25519:ffffffffffffffffffffffffffffffff",
            capability="x.y@v1")
        out = orchestration.resolve_task(conn, created["task_id"])
        assert out["status"] == "FAILED"
        assert out["error_code"] == "AGENT_NOT_FOUND"
    finally:
        conn.close()


def test_remote_dispatch_envelope_and_correlation(db_env):
    from app import delegations, orchestration, tasks
    from app.a2a import service

    conn = _conn(db_env)
    try:
        manager, peer, _ = _world(conn)
        service.create_rule(conn, peer="*", action="*")
        grant = delegations.issue_grant(
            conn, SECRET, "manager", peer.agent_id,
            "faraway.survey@v1", "answer", ttl_seconds=3600)
        created = tasks.create_task(
            conn, requesting_agent_id=manager["id"],
            target_agent=peer.agent_id,
            capability="faraway.survey@v1",
            task_input={"question": "survey says?"},
            delegation_id=grant["id"])
        final, envelope = _run(orchestration.run_task(
            conn, created["task_id"], secret=SECRET))
        assert final["status"] == "DISPATCHED"
        assert envelope is not None
        assert envelope["payload"]["task_id"] == created["task_id"]
        assert envelope["payload"]["capability_id"] == \
            "faraway.survey@v1"
        assert envelope["payload"]["delegation_id"] == grant["id"]
        assert envelope["sender"] == manager["agent_id"]
        assert envelope["recipient"] == peer.agent_id
    finally:
        conn.close()


def test_response_records_remote_task_id(db_env):
    from app import delegations, orchestration
    from app.a2a import service
    from app.identity import crypto

    conn = _conn(db_env)
    try:
        manager, peer, peer_priv = _world(conn)
        service.create_rule(conn, peer="*", action="*")
        grant = delegations.issue_grant(
            conn, SECRET, "manager", peer.agent_id,
            "faraway.survey@v1", "answer", ttl_seconds=3600)
        from app import tasks

        created = tasks.create_task(
            conn, requesting_agent_id=manager["id"],
            target_agent=peer.agent_id,
            capability="faraway.survey@v1",
            delegation_id=grant["id"])
        final, _ = _run(orchestration.run_task(
            conn, created["task_id"], secret=SECRET))
        assert final["status"] == "DISPATCHED"
        from app.a2a import envelope as app_envelope

        # Crafted directly (as B's host would sign it) to avoid same-DB
        # send/store colliding with receive on this single test DB.
        answered = app_envelope.sign(
            app_envelope.new_envelope(
                sender=peer.agent_id, recipient=manager["agent_id"],
                message_type="response",
                payload={"action": "answer", "answer": "surveyed",
                         "remote_task_id": "b-task-9"},
                correlation_id=final["correlation_id"]),
            peer_priv)
        mgr_row = conn.execute(
            "SELECT encrypted_private_key FROM agent_identities"
            " WHERE agent_id = ? AND status = 'active'",
            (manager["agent_id"],)).fetchone()
        mgr_priv = crypto.load_private_key(crypto.decrypt_private_key(
            mgr_row["encrypted_private_key"], SECRET))
        # NOTE: recipient here is the manager AGENT id (local routing).
        out = service.receive_envelope(
            conn, answered, signer_priv=mgr_priv,
            local_id=manager["agent_id"])
        assert out["outcome"] == "answered"
        done = tasks.get_task(conn, final["task_id"])
        assert done["status"] == "COMPLETED"
        assert done["remote_task_id"] == "b-task-9"
    finally:
        conn.close()


def test_remote_error_maps_with_origin(db_env):
    from app import delegations, orchestration
    from app.a2a import service
    from app.a2a.service import A2AError
    from app.identity import crypto
    from app.a2a import envelope as app_envelope

    conn = _conn(db_env)
    try:
        manager, peer, peer_priv = _world(conn)
        service.create_rule(conn, peer="*", action="*")
        grant = delegations.issue_grant(
            conn, SECRET, "manager", peer.agent_id,
            "faraway.survey@v1", "answer", ttl_seconds=3600)
        from app import tasks

        created = tasks.create_task(
            conn, requesting_agent_id=manager["id"],
            target_agent=peer.agent_id,
            capability="faraway.survey@v1",
            delegation_id=grant["id"])
        final, _ = _run(orchestration.run_task(
            conn, created["task_id"], secret=SECRET))
        assert final["status"] == "DISPATCHED"
        err_env = app_envelope.sign(
            app_envelope.new_envelope(
                sender=peer.agent_id, recipient=manager["agent_id"],
                message_type="error",
                payload={"code": "BUSY", "message": "overloaded"},
                correlation_id=final["correlation_id"]),
            peer_priv)
        mgr_row = conn.execute(
            "SELECT encrypted_private_key FROM agent_identities"
            " WHERE agent_id = ? AND status = 'active'",
            (manager["agent_id"],)).fetchone()
        mgr_priv = crypto.load_private_key(crypto.decrypt_private_key(
            mgr_row["encrypted_private_key"], SECRET))
        out = service.receive_envelope(
            conn, err_env, signer_priv=mgr_priv,
            local_id=manager["agent_id"])
        assert out["outcome"] == "error"
        failed = tasks.get_task(conn, final["task_id"])
        assert failed["status"] == "FAILED"
        assert failed["error_code"] == "REMOTE_BUSY"
        assert peer.agent_id in failed["error_detail"]
    finally:
        conn.close()


def test_stranger_error_cannot_fail_task(db_env):
    from app import tasks
    from app.a2a import service
    from app.identity import crypto
    from app.a2a import envelope as app_envelope

    conn = _conn(db_env)
    try:
        manager, peer, peer_priv = _world(conn)
        from app import agents

        stranger = agents.create_agent(conn, SECRET, "stranger")
        s_priv, _ = agents.resolve_signing_key(conn, SECRET, "stranger")
        created = tasks.create_task(
            conn, requesting_agent_id=manager["id"],
            target_agent=peer.agent_id)
        # Stranger forges an error naming our correlation.
        err_env = app_envelope.sign(
            app_envelope.new_envelope(
                sender=stranger["agent_id"], recipient=manager["agent_id"],
                message_type="error",
                payload={"code": "BUSY", "message": "lies"},
                correlation_id=created["correlation_id"]),
            s_priv)
        mgr_row = conn.execute(
            "SELECT encrypted_private_key FROM agent_identities"
            " WHERE agent_id = ? AND status = 'active'",
            (manager["agent_id"],)).fetchone()
        mgr_priv = crypto.load_private_key(crypto.decrypt_private_key(
            mgr_row["encrypted_private_key"], SECRET))
        # Stranger is a LOCAL agent: signature verifies, but the error
        # targets a task bound to another peer → ignored, task lives.
        out = service.receive_envelope(
            conn, err_env, signer_priv=mgr_priv,
            local_id=manager["agent_id"])
        assert out["outcome"] == "error"
        assert tasks.get_task(
            conn, created["task_id"])["status"] == "PENDING"
    finally:
        conn.close()
