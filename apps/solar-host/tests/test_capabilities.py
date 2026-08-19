"""Host-derived capabilities for the /v1/models advertisement.

SGLang upstreams advertise no capabilities (no Ollama-style models array),
so the host derives them from the model directory and reports them
alongside the instance; solar-control stamps them onto the listing.
"""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from solar_host.main import app
from solar_host.models.capabilities import capabilities_for_config
from solar_host.models.huggingface import (
    HuggingFaceEmbeddingConfig,
    HuggingFaceVisionConfig,
)
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.models.sglang import SglangConfig

API_KEY = "test-capabilities-key"

VISION_CAPS = ["completion", "multimodal"]


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


def _write_config(model_dir: Path, config: dict) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def test_an_sglang_model_with_vision_config_is_multimodal(tmp_path) -> None:
    _write_config(tmp_path / "m", {"model_type": "qwen3_moe", "vision_config": {}})
    config = SglangConfig(alias="qwen3.6:35b", model_path=str(tmp_path / "m"))

    assert capabilities_for_config(config) == VISION_CAPS


def test_an_sglang_model_with_a_vl_model_type_is_multimodal(tmp_path) -> None:
    _write_config(tmp_path / "m", {"model_type": "qwen3_vl"})
    config = SglangConfig(alias="qwen3.6:35b", model_path=str(tmp_path / "m"))

    assert capabilities_for_config(config) == VISION_CAPS


def test_a_text_sglang_model_stays_quiet(tmp_path) -> None:
    _write_config(tmp_path / "m", {"model_type": "qwen3_moe"})
    config = SglangConfig(alias="qwen3.6:35b", model_path=str(tmp_path / "m"))

    assert capabilities_for_config(config) is None


def test_a_model_without_config_json_stays_quiet(tmp_path) -> None:
    (tmp_path / "m").mkdir()
    config = SglangConfig(alias="qwen3.6:35b", model_path=str(tmp_path / "m"))

    assert capabilities_for_config(config) is None


def test_local_source_variant(tmp_path) -> None:
    _write_config(tmp_path / "m", {"model_type": "qwen3_vl"})
    config = SglangConfig(alias="qwen3.6:35b", model_source=f"local://{tmp_path / 'm'}")

    assert capabilities_for_config(config) == VISION_CAPS


def test_llamacpp_with_mmproj_is_multimodal() -> None:
    config = LlamaCppConfig(
        alias="qwen3.8:27b", model="/tmp/test.gguf", mmproj="mmproj-BF16.gguf"
    )

    assert capabilities_for_config(config) == VISION_CAPS


def test_llamacpp_without_mmproj_stays_quiet() -> None:
    config = LlamaCppConfig(alias="qwen3.8:27b", model="/tmp/test.gguf")

    assert capabilities_for_config(config) is None


def test_huggingface_vision_is_multimodal() -> None:
    config = HuggingFaceVisionConfig(alias="qwen-vl:7b", model_id="/tmp/m")

    assert capabilities_for_config(config) == VISION_CAPS


def test_an_embedding_backend_stays_quiet() -> None:
    config = HuggingFaceEmbeddingConfig(
        alias="embed:minilm", model_id="sentence-transformers/all-MiniLM-L6-v2"
    )

    assert capabilities_for_config(config) is None


def test_an_sglang_instance_reports_capabilities_at_create(tmp_path) -> None:
    """Mirror of the served-name contract: the report exists from creation on."""
    _write_config(tmp_path / "m", {"model_type": "qwen3_moe", "vision_config": {}})

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/instances",
            headers={"X-API-Key": API_KEY},
            json={
                "config": {
                    "backend_type": "sglang",
                    "model_path": str(tmp_path / "m"),
                    "alias": "qwen3.6:35b",
                }
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["instance"]["capabilities"] == VISION_CAPS


def test_a_text_sglang_instance_reports_no_capabilities(tmp_path) -> None:
    _write_config(tmp_path / "m", {"model_type": "qwen3_moe"})

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/instances",
            headers={"X-API-Key": API_KEY},
            json={
                "config": {
                    "backend_type": "sglang",
                    "model_path": str(tmp_path / "m"),
                    "alias": "qwen3.6:35b",
                }
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["instance"]["capabilities"] is None
