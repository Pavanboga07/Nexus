"""Gateway directory client: verified cards only, transport faults typed."""

from __future__ import annotations

import pytest


def _card(priv, agent_id, pub_b64, **over):
    from relay.directory import sign_card
    from relay.envelope import utc_iso_in, utc_now_iso

    base = {
        "type": "agent-card", "protocol": "nexus-a2a", "version": "0.3",
        "agent_id": agent_id, "display_name": "Remote",
        "public_key": pub_b64, "endpoint": "ws://remote.invalid/ws",
        "capabilities": [{"id": "remote.survey@v1", "version": 1}],
        "supported_purposes": [], "issued_at": utc_now_iso(),
        "expires_at": utc_iso_in(3600),
    }
    base.update(over)
    return sign_card(priv, base)


def _identity():
    from app.identity import crypto
    import base64

    priv, pub = crypto.generate_keypair()
    raw = crypto.public_key_bytes(pub)
    return (priv, crypto.agent_id_from_public_key(raw),
            base64.b64encode(raw).decode("ascii"))


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_fetch_valid_card(monkeypatch):
    import httpx

    from app import remote_directory

    priv, agent_id, pub = _identity()
    card = _card(priv, agent_id, pub)
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _Resp(200, {"card": card}))
    out = remote_directory.fetch_card(
        agent_id, base_url="https://gateway.invalid")
    assert out["agent_id"] == agent_id
    assert out["capabilities"] == [{"id": "remote.survey@v1", "version": 1}]


def test_fetch_forged_card_rejected(monkeypatch):
    import httpx

    from app import remote_directory

    priv, agent_id, pub = _identity()
    card = _card(priv, agent_id, pub)
    card["display_name"] = "Mallory"  # tamper after signing
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _Resp(200, {"card": card}))
    with pytest.raises(remote_directory.DirectoryError) as exc:
        remote_directory.fetch_card(agent_id,
                                    base_url="https://gateway.invalid")
    assert exc.value.code == "INVALID_CARD"


def test_fetch_expired_card_rejected(monkeypatch):
    import httpx

    from app import remote_directory

    priv, agent_id, pub = _identity()
    card = _card(priv, agent_id, pub, expires_at="2000-01-01T00:00:00Z",
                 issued_at="1999-01-01T00:00:00Z")
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _Resp(200, {"card": card}))
    with pytest.raises(remote_directory.DirectoryError) as exc:
        remote_directory.fetch_card(agent_id,
                                    base_url="https://gateway.invalid")
    assert exc.value.code == "INVALID_CARD"


def test_fetch_not_found_and_unreachable(monkeypatch):
    import httpx

    from app import remote_directory

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(404, {}))
    with pytest.raises(remote_directory.DirectoryError) as exc:
        remote_directory.fetch_card(
            "nexus:ed25519:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            base_url="https://gateway.invalid")
    assert exc.value.code == "NOT_FOUND"

    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _boom)
    with pytest.raises(remote_directory.DirectoryError) as exc:
        remote_directory.fetch_card(
            "nexus:ed25519:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            base_url="https://gateway.invalid")
    assert exc.value.code == "GATEWAY_UNREACHABLE"


def test_fetch_rejects_bad_agent_id():
    from app import remote_directory

    with pytest.raises(remote_directory.DirectoryError) as exc:
        remote_directory.fetch_card("not-an-id",
                                    base_url="https://gateway.invalid")
    assert exc.value.code == "BAD_AGENT_ID"


def test_search_verifies_and_counts_rejections(monkeypatch):
    import httpx

    from app import remote_directory

    priv, agent_id, pub = _identity()
    good = _card(priv, agent_id, pub)
    bad = dict(good)
    bad["signature"] = "AAAA"
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _Resp(200, {"items": [
            {"agent_id": agent_id, "card": good},
            {"agent_id": "x", "card": bad},
            {"agent_id": "y"},
        ]}))
    out = remote_directory.search_directory(
        10, base_url="https://gateway.invalid")
    assert [c["agent_id"] for c in out["entries"]] == [agent_id]
    assert out["rejected"] == 2


def test_search_params_encoded(monkeypatch):
    import httpx

    from app import remote_directory

    seen = {}

    def _get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _Resp(200, {"items": []})

    monkeypatch.setattr(httpx, "get", _get)
    remote_directory.search_directory(20, base_url="https://gateway.invalid")
    assert seen["url"] == "https://gateway.invalid/directory"
    assert seen["params"] == {"limit": 20, "offset": 0}


def test_gateway_base_validation(monkeypatch):
    from app import remote_directory

    monkeypatch.setenv("NEXUS_RELAY_URL",
                       "wss://gateway.example.com")
    assert remote_directory.gateway_http_base() == \
        "https://gateway.example.com"
    monkeypatch.setenv("NEXUS_RELAY_URL", "ftp://evil.example/x")
    with pytest.raises(remote_directory.DirectoryError) as exc:
        remote_directory.gateway_http_base()
    assert exc.value.code == "BAD_GATEWAY_URL"
