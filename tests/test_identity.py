"""V1 identity tests (TDD red first).

Covers Task V1 gate: keygen randomness, deterministic agent_id,
fingerprint shape, sign/verify round-trip, tamper failure, key mismatch,
encrypted-at-rest, reload integrity, corruption fails loudly.
"""

import base64
import re
import sqlite3

import pytest

from app.identity import crypto
from app.identity.service import (
    IdentityCorruptionError,
    ensure_identity,
    load_identity,
    sign_data,
)
from app.store import migrate, open_db


def test_keygen_produces_32_byte_keys_and_each_run_differs():
    priv1, pub1 = crypto.generate_keypair()
    priv2, pub2 = crypto.generate_keypair()
    raw_priv1 = crypto.private_key_bytes(priv1)
    raw_pub1 = crypto.public_key_bytes(pub1)
    raw_priv2 = crypto.private_key_bytes(priv2)
    raw_pub2 = crypto.public_key_bytes(pub2)
    assert len(raw_priv1) == 32
    assert len(raw_pub1) == 32
    assert len(raw_priv2) == 32
    assert len(raw_pub2) == 32
    # Gate: no two runs produce the same key.
    assert raw_priv1 != raw_priv2
    assert raw_pub1 != raw_pub2


def test_agent_id_deterministic_from_public_key():
    _, pub = crypto.generate_keypair()
    raw = crypto.public_key_bytes(pub)
    id1 = crypto.agent_id_from_public_key(raw)
    id2 = crypto.agent_id_from_public_key(raw)
    assert id1 == id2
    assert id1.startswith("nexus:ed25519:")
    hexpart = id1.removeprefix("nexus:ed25519:")
    assert len(hexpart) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", hexpart)


def test_fingerprint_shape_is_8x4_upper_hex():
    _, pub = crypto.generate_keypair()
    raw = crypto.public_key_bytes(pub)
    fp1 = crypto.fingerprint_from_public_key(raw)
    fp2 = crypto.fingerprint_from_public_key(raw)
    assert fp1 == fp2
    assert re.fullmatch(r"[0-9A-F]{4}(-[0-9A-F]{4}){7}", fp1)


def test_sign_verify_round_trip():
    priv, pub = crypto.generate_keypair()
    data = b"nexus self-certification probe"
    sig = crypto.sign_bytes(priv, data)
    assert crypto.verify_bytes(pub, data, sig) is True


def test_tampered_message_fails_verify():
    priv, pub = crypto.generate_keypair()
    data = b"the quick brown fox"
    sig = crypto.sign_bytes(priv, data)
    assert crypto.verify_bytes(pub, b"the quick brown fox!", sig) is False
    assert crypto.verify_bytes(pub, data, sig[:-1] + bytes([sig[-1] ^ 0x01])) is False


def test_wrong_key_mismatch_detectable():
    priv_a, pub_a = crypto.generate_keypair()
    _, pub_b = crypto.generate_keypair()
    raw_a = crypto.public_key_bytes(pub_a)
    raw_b = crypto.public_key_bytes(pub_b)
    # Different keys -> different self-certifying ids.
    assert crypto.agent_id_from_public_key(raw_a) != crypto.agent_id_from_public_key(
        raw_b
    )
    # Signature from A does not verify under B.
    sig = crypto.sign_bytes(priv_a, b"hello")
    assert crypto.verify_bytes(pub_b, b"hello", sig) is False
    # Private half of A does not match public half of B.
    assert crypto.keypair_matches(priv_a, pub_a) is True
    assert crypto.keypair_matches(priv_a, pub_b) is False


def _open_tmp_db(tmp_path):
    path = tmp_path / "v1-identity.db"
    conn = open_db(str(path))
    migrate(conn)
    return conn, path


def test_private_key_encrypted_at_rest(tmp_path):
    conn, _ = _open_tmp_db(tmp_path)
    secret = "test-machine-secret-aaaaaaaaaaaaaaaa"
    ident = ensure_identity(conn, secret)
    row = conn.execute(
        "SELECT public_key, encrypted_private_key, agent_id FROM identity WHERE id = 1"
    ).fetchone()
    assert row is not None
    assert row["agent_id"] == ident.agent_id
    stored_pub_raw = base64.b64decode(row["public_key"])
    # Ciphertext blob must not contain the raw private key bytes anywhere.
    blob_b64 = row["encrypted_private_key"]
    blob = base64.b64decode(blob_b64)
    assert blob_b64 != base64.b64encode(stored_pub_raw).decode("ascii")
    # Decrypt via crypto layer to get the raw private bytes, then prove the
    # at-rest blob is not plaintext.
    raw_priv = crypto.decrypt_private_key(blob_b64, secret)
    assert len(raw_priv) == 32
    assert raw_priv not in blob
    assert raw_priv != blob
    conn.close()


def test_reload_integrity_same_identity_and_signature_verifies(tmp_path):
    conn, path = _open_tmp_db(tmp_path)
    secret = "test-machine-secret-bbbbbbbbbbbbbbbb"
    first = ensure_identity(conn, secret)
    sig = sign_data(conn, secret, b"reload probe")
    conn.close()

    conn2 = open_db(str(path))
    migrate(conn2)
    second = load_identity(conn2, secret)
    assert second.agent_id == first.agent_id
    assert second.public_key == first.public_key
    assert second.fingerprint == first.fingerprint
    pub_raw = base64.b64decode(second.public_key)
    assert crypto.verify_bytes(crypto.load_public_key(pub_raw), b"reload probe", sig)
    # ensure_identity is idempotent: no rotation on reload.
    third = ensure_identity(conn2, secret)
    assert third.agent_id == first.agent_id
    conn2.close()


def test_corruption_fails_loudly(tmp_path):
    conn, _ = _open_tmp_db(tmp_path)
    secret = "test-machine-secret-cccccccccccccccc"
    ensure_identity(conn, secret)
    row = conn.execute("SELECT encrypted_private_key FROM identity WHERE id = 1").fetchone()
    blob = row["encrypted_private_key"]
    # Flip a trailing base64 char to corrupt the GCM ciphertext/tag.
    bad_tail = "A" if not blob.endswith("A") else "B"
    corrupted = blob[:-1] + bad_tail
    assert corrupted != blob
    conn.execute(
        "UPDATE identity SET encrypted_private_key = ? WHERE id = 1", (corrupted,)
    )
    conn.commit()
    with pytest.raises(IdentityCorruptionError):
        load_identity(conn, secret)
    conn.close()


def test_wrong_secret_fails_loudly(tmp_path):
    conn, _ = _open_tmp_db(tmp_path)
    ensure_identity(conn, "correct-machine-secret-dddddddddddd")
    with pytest.raises(IdentityCorruptionError):
        load_identity(conn, "wrong-machine-secret-eeeeeeeeeeee")
    conn.close()
