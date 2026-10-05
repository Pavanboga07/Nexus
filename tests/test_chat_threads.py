"""Chat thread endpoints (V8): history persists per session_id.

Streams through the real SSE route (patched provider), then proves the
thread list / detail / delete contract the history sidebar consumes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.conftest import auth_headers


def scripted(chunks):
    async def _gen(payload):
        for chunk in chunks:
            yield chunk

    return _gen


def make_provider(**kwargs):
    from app.llm.provider import OpenAICompatibleProvider

    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("model", "test-model")
    return OpenAICompatibleProvider(**kwargs)


def make_client(monkeypatch, db, text="threaded answer"):
    import app.api.routes.chat as chat_route
    from app.main import app

    monkeypatch.setenv("NEXUS_DB_PATH", db)
    monkeypatch.setattr(
        chat_route,
        "build_provider",
        lambda: make_provider(post_stream_fn=scripted([{"content": text}])),
    )
    monkeypatch.setattr(chat_route, "build_tools", lambda: {})
    return TestClient(app, headers=auth_headers())


def test_stream_persists_and_threads_round_trip(tmp_path, monkeypatch):
    client = make_client(monkeypatch, str(tmp_path / "th.db"))
    with client.stream(
        "GET", "/chat/stream", params={"message": "remember me", "session_id": "s1"}
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")
    assert '"type": "done"' in body or '"type":"done"' in body

    listed = client.get("/chat/threads")
    assert listed.status_code == 200
    threads = listed.json()["threads"]
    assert len(threads) == 1
    assert threads[0]["thread_id"] == "s1"
    assert "remember me" in threads[0]["title"]

    detail = client.get("/chat/threads/s1")
    assert detail.status_code == 200
    turns = detail.json()["turns"]
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "remember me"),
        ("assistant", "threaded answer"),
    ]

    deleted = client.delete("/chat/threads/s1")
    assert deleted.status_code == 200
    assert deleted.json() == {"id": "s1", "deleted": True}
    assert client.get("/chat/threads").json() == {"threads": []}


def test_thread_404s(tmp_path, monkeypatch):
    client = make_client(monkeypatch, str(tmp_path / "th404.db"))
    assert client.get("/chat/threads/nope").status_code == 404
    assert client.delete("/chat/threads/nope").status_code == 404


def test_second_stream_appends_to_same_thread(tmp_path, monkeypatch):
    client = make_client(monkeypatch, str(tmp_path / "th2.db"))
    for text in ("first", "second"):
        with client.stream(
            "GET",
            "/chat/stream",
            params={"message": text, "session_id": "s9"},
        ) as response:
            assert response.status_code == 200
            response.read()
    turns = client.get("/chat/threads/s9").json()["turns"]
    assert [t["text"] for t in turns if t["role"] == "user"] == [
        "first",
        "second",
    ]
