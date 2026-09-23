"""CORS regression: local frontend (:3001) must get ACAO headers from API."""

from fastapi.testclient import TestClient

from app.main import app


def test_cross_origin_get_returns_allow_origin():
    client = TestClient(app)
    for origin in ("http://localhost:3001", "http://127.0.0.1:3001"):
        resp = client.get("/health", headers={"Origin": origin})
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == origin
