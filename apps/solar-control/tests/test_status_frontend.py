"""Tests for the embedded status SPA serving at /status.

The frontend router registers FastAPI's low-priority static serving only
when the built SPA dist directory exists at startup, so a checkout without
a SPA build starts cleanly and /status 404s. Regular path operations always
take precedence over the frontend mount.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import auth_middleware
from app.config import settings
from app.routes.frontend import create_frontend_router

INDEX_HTML = "<!doctype html><html><body>status SPA</body></html>"
APP_JS = "console.log('status');"


def _build_app() -> FastAPI:
    """Mirror main.py wiring for hermetic tests with a controlled dist."""
    test_app = FastAPI()
    test_app.middleware("http")(auth_middleware)
    test_app.include_router(create_frontend_router(), prefix="/status")

    @test_app.get("/api/ping")
    async def ping():
        return {"ok": True}

    @test_app.get("/status/healthz")
    async def status_healthz():
        return {"ok": True}

    return test_app


def _write_dist(dist_dir):
    dist_dir.mkdir()
    (dist_dir / "assets").mkdir()
    (dist_dir / "index.html").write_text(INDEX_HTML)
    (dist_dir / "assets" / "app.js").write_text(APP_JS)


def test_frontend_served_when_dist_exists(monkeypatch, tmp_path):
    dist_dir = tmp_path / "dist"
    _write_dist(dist_dir)
    monkeypatch.setattr(settings, "status_dist_dir", dist_dir)

    client = TestClient(_build_app())

    resp = client.get("/status", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert INDEX_HTML in resp.text


def test_client_side_route_falls_back_to_index(monkeypatch, tmp_path):
    dist_dir = tmp_path / "dist"
    _write_dist(dist_dir)
    monkeypatch.setattr(settings, "status_dist_dir", dist_dir)

    client = TestClient(_build_app())

    resp = client.get("/status/hosts/abc123", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert INDEX_HTML in resp.text


def test_existing_assets_served_and_missing_assets_404(monkeypatch, tmp_path):
    dist_dir = tmp_path / "dist"
    _write_dist(dist_dir)
    monkeypatch.setattr(settings, "status_dist_dir", dist_dir)

    client = TestClient(_build_app())

    resp = client.get("/status/assets/app.js")
    assert resp.status_code == 200
    assert APP_JS in resp.text

    resp = client.get("/status/assets/missing.js")
    assert resp.status_code == 404


def test_frontend_not_registered_without_dist(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "status_dist_dir", tmp_path / "missing")

    client = TestClient(_build_app())

    resp = client.get("/status", headers={"Accept": "text/html"})
    assert resp.status_code == 404


def test_regular_routes_take_precedence_over_frontend(monkeypatch, tmp_path):
    dist_dir = tmp_path / "dist"
    _write_dist(dist_dir)
    monkeypatch.setattr(settings, "status_dist_dir", dist_dir)

    client = TestClient(_build_app())

    resp = client.get("/status/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_status_page_is_public_and_api_stays_gated(monkeypatch, tmp_path):
    dist_dir = tmp_path / "dist"
    _write_dist(dist_dir)
    monkeypatch.setattr(settings, "status_dist_dir", dist_dir)

    client = TestClient(_build_app())

    resp = client.get("/status", headers={"Accept": "text/html"})
    assert resp.status_code == 200

    resp = client.get("/api/ping")
    assert resp.status_code == 401

    resp = client.get("/api/ping", headers={"X-API-Key": settings.management_api_key})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
