"""The served model name a host reports for an instance.

solar-control routes on the alias but has to send the backend the name it was
actually launched with, so the host reports that name per instance.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from solar_host.main import app

API_KEY = "test-served-name-key"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch):
    """Own config store: the manager is a singleton built at import time, so
    patching the setting alone would leave it writing the developer's file."""
    from solar_host.config import config_manager

    monkeypatch.setattr(
        "solar_host.config.settings.config_file", str(tmp_path / "config.json")
    )
    monkeypatch.setattr("solar_host.config.settings.solar_control_url", "")
    monkeypatch.setattr("solar_host.config.settings.api_key", API_KEY)
    monkeypatch.setattr(config_manager, "config_file", tmp_path / "config.json")
    monkeypatch.setattr(config_manager, "instances", {})


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _create(client: TestClient, config: dict) -> dict:
    resp = client.post(
        "/instances", headers={"X-API-Key": API_KEY}, json={"config": config}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["instance"]


def test_a_llamacpp_instance_is_served_under_its_alias(client: TestClient) -> None:
    instance = _create(
        client,
        {
            "backend_type": "llamacpp",
            "model": "/tmp/test.gguf",
            "alias": "qwen3.6:35b",
        },
    )

    assert instance["served_model_name"] == "qwen3.6:35b"


def test_an_sglang_instance_reports_its_colon_free_name(client: TestClient) -> None:
    """Reported at creation, so control can translate from the first request on."""
    instance = _create(
        client,
        {
            "backend_type": "sglang",
            "model_path": "/models/dsv4",
            "alias": "deepseek-v4-flash:284b",
        },
    )

    assert instance["served_model_name"] == "deepseek-v4-flash-284b"

    listed = client.get("/instances", headers={"X-API-Key": API_KEY}).json()
    assert listed[0]["served_model_name"] == "deepseek-v4-flash-284b"
