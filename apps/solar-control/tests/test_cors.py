"""Tests for the configurable CORS allow-list on solar-control.

CORS_ORIGINS is a comma-separated origin allow-list; unset or empty means
the CORS middleware is not installed at all and cross-origin requests get
no Access-Control-* headers (the production deployment is same-origin).
The dev-mode status SPA opt in locally via the env var.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.main import configure_cors


def _build_app() -> FastAPI:
    test_app = FastAPI()

    @test_app.get("/ping")
    async def ping():
        return {"ok": True}

    return test_app


def _client(monkeypatch, cors_origins: str) -> TestClient:
    monkeypatch.setattr(settings, "cors_origins", cors_origins)
    app = _build_app()
    configure_cors(app)
    return TestClient(app)


def test_cors_origin_list_parses_csv(monkeypatch):
    monkeypatch.setattr(
        settings,
        "cors_origins",
        " http://a.example , ,http://b.example, ",
    )
    assert settings.cors_origin_list == ["http://a.example", "http://b.example"]


def test_cors_disabled_by_default(monkeypatch):
    client = _client(monkeypatch, "")

    resp = client.get("/ping", headers={"Origin": "http://localhost:5174"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_disabled_for_whitespace_only_value(monkeypatch):
    client = _client(monkeypatch, "   ")

    resp = client.get("/ping", headers={"Origin": "http://localhost:5174"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_allows_listed_origin(monkeypatch):
    client = _client(
        monkeypatch,
        "http://localhost:5174, http://localhost:5173",
    )

    resp = client.get("/ping", headers={"Origin": "http://localhost:5174"})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5174"


def test_cors_rejects_unlisted_origin(monkeypatch):
    client = _client(monkeypatch, "http://localhost:5174")

    resp = client.get("/ping", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_preflight_allowed_for_listed_origin(monkeypatch):
    client = _client(monkeypatch, "http://localhost:5174")

    resp = client.options(
        "/ping",
        headers={
            "Origin": "http://localhost:5174",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-api-key",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5174"
    assert "x-api-key" in resp.headers["access-control-allow-headers"].lower()
