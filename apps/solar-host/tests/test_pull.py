"""Tests for POST /models/pull endpoint (solar_host/routes/models.py).

All external I/O (Harbor OrasHelper, huggingface_hub.snapshot_download) is
mocked. Filesystem operations use tmp_path so nothing touches the real disk.
"""

import hashlib
import itertools
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from solar_host.main import app
from solar_host.models_manager import (
    ManifestEntry,
    add_manifest_entry,
    ensure_models_dir,
    get_manifest_entry,
    get_models_dir,
)

API_KEY = "test-key-s015"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch):
    """Point settings at a fresh tmp models dir and fixed API key/credentials.

    Credentials are set to valid values by default; individual tests can
    override them to trigger credential-missing errors.
    """
    models = tmp_path / "models"
    monkeypatch.setattr("solar_host.config.settings.models_dir", str(models))
    monkeypatch.setattr("solar_host.config.settings.solar_control_url", "")
    monkeypatch.setattr("solar_host.config.settings.api_key", API_KEY)
    monkeypatch.setattr(
        "solar_host.config.settings.harbor_url", "https://imgrepo.damit.hu"
    )
    monkeypatch.setattr("solar_host.config.settings.harbor_username", "robot")
    monkeypatch.setattr("solar_host.config.settings.harbor_password", "secret")
    monkeypatch.setattr("solar_host.config.settings.hf_token", "")
    # In-process pulls so mocks on _pull_* apply; low threshold so tmp disks pass.
    monkeypatch.setattr("solar_host.config.settings.pull_use_subprocess", False)
    monkeypatch.setattr("solar_host.config.settings.pull_disk_poll_interval_s", 0.05)
    monkeypatch.setattr("solar_host.config.settings.min_free_disk_gb", 0.001)
    return models


@pytest.fixture()
def client():
    """HTTP test client with full app lifespan."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _headers() -> dict:
    return {"X-API-Key": API_KEY}


def _harbor_body(**overrides) -> dict:
    defaults = {
        "source": "harbor",
        "source_uri": "repo://iris-osl:v3",
        "harbor_ref": "imgrepo.damit.hu/supernova/iris-osl:v3",
        "digest": "sha256:abc123",
    }
    defaults.update(overrides)
    return defaults


def _hf_body(**overrides) -> dict:
    defaults = {
        "source": "huggingface",
        "source_uri": "huggingface://microsoft/phi-3",
        "model_id": "microsoft/phi-3",
    }
    defaults.update(overrides)
    return defaults


def _make_manifest_entry(**overrides) -> ManifestEntry:
    defaults = {
        "slug": "repo--iris-osl--v3",
        "source_uri": "repo://iris-osl:v3",
        "path": "/opt/solar/models/repo--iris-osl--v3",
        "size_bytes": 4815162342,
        "digest": "sha256:abc123",
        "downloaded_at": "2026-03-31T14:22:00Z",
    }
    defaults.update(overrides)
    return ManifestEntry(**defaults)


# ---------------------------------------------------------------------------
# Proactive disk space (S-018)
# ---------------------------------------------------------------------------


class TestProactiveDiskSpace:
    def test_returns_507_when_size_bytes_exceeds_available(
        self, client: TestClient, _isolated_env: Path
    ):
        with patch(
            "solar_host.models_manager.get_disk_info",
            return_value={"available_gb": 1.0, "used_gb": 1.0, "total_gb": 2.0},
        ):
            body = {**_harbor_body(), "size_bytes": 200 * 1024**3}
            resp = client.post("/models/pull", json=body, headers=_headers())
        assert resp.status_code == 507
        detail = resp.json()["detail"]
        assert "Insufficient disk space" in detail
        assert "1.00" in detail
        assert "200.00" in detail

    def test_returns_507_when_unknown_size_below_min_free(
        self, client: TestClient, _isolated_env: Path, monkeypatch
    ):
        monkeypatch.setattr("solar_host.config.settings.min_free_disk_gb", 100.0)
        with patch(
            "solar_host.models_manager.get_disk_info",
            return_value={"available_gb": 5.0, "used_gb": 1.0, "total_gb": 6.0},
        ):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 507
        assert "100.00" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_api_key_returns_401(self, client: TestClient):
        resp = client.post("/models/pull", json=_harbor_body())
        assert resp.status_code == 401

    def test_wrong_api_key_returns_401(self, client: TestClient):
        resp = client.post(
            "/models/pull", json=_harbor_body(), headers={"X-API-Key": "wrong"}
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Request validation (422)
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_harbor_source_requires_harbor_ref(self, client: TestClient):
        body = _harbor_body()
        del body["harbor_ref"]
        resp = client.post("/models/pull", json=body, headers=_headers())
        assert resp.status_code == 422

    def test_huggingface_source_requires_model_id(self, client: TestClient):
        body = _hf_body()
        del body["model_id"]
        resp = client.post("/models/pull", json=body, headers=_headers())
        assert resp.status_code == 422

    def test_invalid_source_type_returns_422(self, client: TestClient):
        resp = client.post(
            "/models/pull",
            json={"source": "s3", "source_uri": "s3://bucket/model"},
            headers=_headers(),
        )
        assert resp.status_code == 422

    def test_harbor_ref_empty_string_returns_422(self, client: TestClient):
        resp = client.post(
            "/models/pull",
            json={**_harbor_body(), "harbor_ref": ""},
            headers=_headers(),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# source_uri / source type mismatch (400)
# ---------------------------------------------------------------------------


class TestSourceUriMismatch:
    def test_harbor_source_with_hf_uri_returns_400(self, client: TestClient):
        resp = client.post(
            "/models/pull",
            json={
                "source": "harbor",
                "source_uri": "huggingface://microsoft/phi-3",
                "harbor_ref": "imgrepo.damit.hu/supernova/phi-3:v1",
            },
            headers=_headers(),
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"] == "invalid_request"
        assert data["source_uri"] == "huggingface://microsoft/phi-3"

    def test_huggingface_source_with_repo_uri_returns_400(self, client: TestClient):
        resp = client.post(
            "/models/pull",
            json={
                "source": "huggingface",
                "source_uri": "repo://iris-osl:v3",
                "model_id": "iris-osl",
            },
            headers=_headers(),
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Cache hit
# ---------------------------------------------------------------------------


class TestCacheHit:
    def test_cache_hit_returns_cached_true(
        self, client: TestClient, _isolated_env: Path
    ):
        ensure_models_dir()
        slug_dir = _isolated_env / "repo--iris-osl--v3"
        slug_dir.mkdir(parents=True, exist_ok=True)
        (slug_dir / "model.gguf").write_bytes(b"x")
        add_manifest_entry(_make_manifest_entry(path=str(slug_dir.resolve())))

        with patch("solar_host.models_manager._pull_harbor") as mock_pull:
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is True
        assert data["source_uri"] == "repo://iris-osl:v3"
        assert data["path"] == str(slug_dir.resolve())
        mock_pull.assert_not_called()

    def test_cache_hit_returns_stored_path(
        self, client: TestClient, _isolated_env: Path
    ):
        ensure_models_dir()
        custom = _isolated_env / "custom-path-to-model"
        custom.mkdir(parents=True, exist_ok=True)
        (custom / "model.gguf").write_bytes(b"x")
        add_manifest_entry(_make_manifest_entry(path=str(custom.resolve())))

        resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.json()["path"] == str(custom.resolve())

    def test_hf_cache_hit(self, client: TestClient, _isolated_env: Path):
        ensure_models_dir()
        slug_dir = _isolated_env / "hf--microsoft--phi-3"
        slug_dir.mkdir(parents=True, exist_ok=True)
        (slug_dir / "config.json").write_bytes(b"{}")
        add_manifest_entry(
            _make_manifest_entry(
                slug="hf--microsoft--phi-3",
                source_uri="huggingface://microsoft/phi-3",
                path=str(slug_dir.resolve()),
            )
        )
        with patch("solar_host.models_manager._pull_huggingface") as mock_dl:
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["cached"] is True
        mock_dl.assert_not_called()

    def test_cache_hit_with_subpath_returns_file_path(
        self, client: TestClient, _isolated_env: Path
    ):
        """Cache hit for repo://name:version/model.gguf returns dir/model.gguf."""
        ensure_models_dir()
        slug_dir = _isolated_env / "repo--iris-osl--v3"
        slug_dir.mkdir(parents=True, exist_ok=True)
        (slug_dir / "model.gguf").write_bytes(b"x")
        add_manifest_entry(_make_manifest_entry(path=str(slug_dir.resolve())))

        body = _harbor_body(source_uri="repo://iris-osl:v3/model.gguf")
        with patch("solar_host.models_manager._pull_harbor") as mock_pull:
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is True
        assert data["path"] == str((slug_dir / "model.gguf").resolve())
        mock_pull.assert_not_called()

    def test_cache_hit_with_subpath_missing_file_re_pulls(
        self, client: TestClient, _isolated_env: Path
    ):
        """Cache entry exists but the subpath file is gone → re-pull."""
        ensure_models_dir()
        slug_dir = _isolated_env / "repo--iris-osl--v3"
        slug_dir.mkdir(parents=True, exist_ok=True)
        (slug_dir / "model.gguf").write_bytes(b"x")
        add_manifest_entry(_make_manifest_entry(path=str(slug_dir.resolve())))

        body = _harbor_body(source_uri="repo://iris-osl:v3/other.gguf")

        def _side_effect(harbor_ref: str, target_dir: Path, source_uri: str):
            target_dir.mkdir(parents=True, exist_ok=True)
            # Re-create the file named by the subpath in source_uri
            from solar_host.models_manager import extract_repo_subpath

            fname = extract_repo_subpath(source_uri)
            (target_dir / fname).write_bytes(b"x" * 1024)

        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=_side_effect,
        ) as mock_pull:
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is False
        assert data["path"] == str((slug_dir / "other.gguf").resolve())
        mock_pull.assert_called_once()


# ---------------------------------------------------------------------------
# GGUF selection for llama.cpp + repo:// artifacts (fix/repo-resolution)
# ---------------------------------------------------------------------------


class TestGgufSelection:
    """llama.cpp + harbor pulls resolve to the largest *.gguf in the artifact.

    Selection applies only when the caller declares backend_type ==
    "llamacpp" on a harbor (repo://) pull without an explicit subpath.
    local:// and huggingface:// pulls always stay directories.
    """

    def _mock_pull_files(self, files: dict[str, bytes]):
        """Side effect for _pull_harbor that writes the given files."""

        def _side_effect(
            harbor_ref: str,
            target_dir: Path,
            source_uri: str,
            allow_patterns: list[str] | None = None,
        ):
            target_dir.mkdir(parents=True, exist_ok=True)
            for name, data in files.items():
                file = target_dir / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)

        return _side_effect

    def test_fresh_pull_single_gguf_resolves_to_file(
        self, client: TestClient, _isolated_env: Path
    ):
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._mock_pull_files({"model.gguf": b"x" * 1024}),
        ) as mock_pull:
            body = _harbor_body(backend_type="llamacpp")
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is False
        assert data["path"].endswith("repo--iris-osl--v3/model.gguf")
        mock_pull.assert_called_once()

    def test_fresh_pull_multiple_ggufs_picks_largest(
        self, client: TestClient, _isolated_env: Path
    ):
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._mock_pull_files(
                {
                    "small.gguf": b"s" * 512,
                    "big.gguf": b"b" * 2048,
                    "README.md": b"readme",
                }
            ),
        ):
            body = _harbor_body(backend_type="llamacpp")
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["path"].endswith("repo--iris-osl--v3/big.gguf")

    def test_fresh_pull_no_gguf_returns_404(
        self, client: TestClient, _isolated_env: Path
    ):
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._mock_pull_files({"config.json": b"{}"}),
        ):
            body = _harbor_body(backend_type="llamacpp")
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 404
        assert "No .gguf file found" in resp.json()["detail"]

    def test_backend_type_omitted_keeps_directory(
        self, client: TestClient, _isolated_env: Path
    ):
        """No backend_type → no selection (existing behavior unchanged)."""
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._mock_pull_files({"model.gguf": b"x" * 1024}),
        ):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["path"].endswith("repo--iris-osl--v3")

    def test_hf_source_with_llamacpp_keeps_directory(
        self, client: TestClient, _isolated_env: Path
    ):
        """huggingface:// artifacts are folders even for llamacpp backend."""
        with patch(
            "solar_host.models_manager._pull_huggingface",
            side_effect=self._mock_pull_files(
                {"model.gguf": b"x" * 1024, "config.json": b"{}"}
            ),
        ):
            body = _hf_body(backend_type="llamacpp")
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["path"].endswith("hf--microsoft--phi-3")

    def test_explicit_subpath_wins_over_selection(
        self, client: TestClient, _isolated_env: Path
    ):
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._mock_pull_files(
                {
                    "small.gguf": b"s" * 512,
                    "big.gguf": b"b" * 2048,
                    "nested/model.gguf": b"n" * 1024,
                }
            ),
        ):
            body = _harbor_body(
                source_uri="repo://iris-osl:v3/small.gguf", backend_type="llamacpp"
            )
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["path"].endswith("repo--iris-osl--v3/small.gguf")

    def test_cache_hit_llamacpp_resolves_to_largest_gguf(
        self, client: TestClient, _isolated_env: Path
    ):
        ensure_models_dir()
        slug_dir = _isolated_env / "repo--iris-osl--v3"
        slug_dir.mkdir(parents=True, exist_ok=True)
        (slug_dir / "small.gguf").write_bytes(b"s" * 512)
        (slug_dir / "big.gguf").write_bytes(b"b" * 2048)
        add_manifest_entry(_make_manifest_entry(path=str(slug_dir.resolve())))

        with patch("solar_host.models_manager._pull_harbor") as mock_pull:
            body = _harbor_body(backend_type="llamacpp")
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is True
        assert data["path"] == str((slug_dir / "big.gguf").resolve())
        mock_pull.assert_not_called()

    def test_cache_hit_llamacpp_no_gguf_returns_404(
        self, client: TestClient, _isolated_env: Path
    ):
        ensure_models_dir()
        slug_dir = _isolated_env / "repo--iris-osl--v3"
        slug_dir.mkdir(parents=True, exist_ok=True)
        (slug_dir / "config.json").write_bytes(b"{}")
        add_manifest_entry(_make_manifest_entry(path=str(slug_dir.resolve())))

        body = _harbor_body(backend_type="llamacpp")
        resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 404
        assert "No .gguf file found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Post-pull digest verification (D-017)
# ---------------------------------------------------------------------------


class _FakeOras:
    """Minimal OrasHelper stand-in: ``_client.get_manifest`` returns layers."""

    def __init__(self, file_digests: dict[str, str], *, fail_manifest: bool = False):
        self._client = _FakeOrasClient(file_digests, fail_manifest=fail_manifest)


class _FakeOrasClient:
    def __init__(self, file_digests: dict[str, str], *, fail_manifest: bool):
        self._file_digests = file_digests
        self._fail_manifest = fail_manifest

    def get_manifest(self, harbor_ref: str) -> dict:
        if self._fail_manifest:
            raise RuntimeError("registry unreachable")
        return {
            "layers": [
                {
                    "digest": f"sha256:{digest}",
                    "annotations": {"org.opencontainers.image.title": name},
                }
                for name, digest in self._file_digests.items()
            ]
        }


class TestDigestVerification:
    """Pulled artifacts are verified against OCI manifest layer digests."""

    def _artifact_dir(self, _isolated_env: Path) -> Path:
        slug_dir = _isolated_env / "repo--iris-osl--v3"
        slug_dir.mkdir(parents=True, exist_ok=True)
        return slug_dir

    def test_verify_pulled_digests_happy_path(self, _isolated_env: Path):
        from solar_host.models_manager import _verify_pulled_digests

        slug_dir = self._artifact_dir(_isolated_env)
        data = b"x" * 1024
        (slug_dir / "model.gguf").write_bytes(data)
        want = hashlib.sha256(data).hexdigest()

        digests = _verify_pulled_digests(
            _FakeOras({"model.gguf": want}),
            "imgrepo.damit.hu/supernova/iris-osl:v3",
            slug_dir,
            "repo://iris-osl:v3",
        )
        assert digests == {"model.gguf": want}

    def test_verify_pulled_digests_detects_tamper(self, _isolated_env: Path):
        from solar_host.models_manager import ModelPullError, _verify_pulled_digests

        slug_dir = self._artifact_dir(_isolated_env)
        (slug_dir / "model.gguf").write_bytes(b"y" * 1024)  # tampered
        want = hashlib.sha256(b"x" * 1024).hexdigest()

        with pytest.raises(ModelPullError) as ei:
            _verify_pulled_digests(
                _FakeOras({"model.gguf": want}),
                "imgrepo.damit.hu/supernova/iris-osl:v3",
                slug_dir,
                "repo://iris-osl:v3",
            )
        assert "integrity check failed" in str(ei.value)
        assert "model.gguf" in str(ei.value)

    def test_verify_pulled_digests_detects_missing_file(self, _isolated_env: Path):
        from solar_host.models_manager import ModelPullError, _verify_pulled_digests

        slug_dir = self._artifact_dir(_isolated_env)  # empty
        want = hashlib.sha256(b"x" * 1024).hexdigest()

        with pytest.raises(ModelPullError) as ei:
            _verify_pulled_digests(
                _FakeOras({"model.gguf": want}),
                "imgrepo.damit.hu/supernova/iris-osl:v3",
                slug_dir,
                "repo://iris-osl:v3",
            )
        assert "model.gguf: missing on disk" in str(ei.value)

    def test_verify_pulled_digests_skips_when_manifest_unavailable(
        self, _isolated_env: Path
    ):
        from solar_host.models_manager import _verify_pulled_digests

        slug_dir = self._artifact_dir(_isolated_env)
        (slug_dir / "model.gguf").write_bytes(b"x" * 1024)

        digests = _verify_pulled_digests(
            _FakeOras({}, fail_manifest=True),
            "imgrepo.damit.hu/supernova/iris-osl:v3",
            slug_dir,
            "repo://iris-osl:v3",
        )
        assert digests is None

    def test_verify_pulled_digests_accepts_nested_paths(self, _isolated_env: Path):
        from solar_host.models_manager import _verify_pulled_digests

        slug_dir = self._artifact_dir(_isolated_env)
        (slug_dir / "nested").mkdir()
        data = b"x" * 1024
        (slug_dir / "nested" / "extra.txt").write_bytes(data)
        want = hashlib.sha256(data).hexdigest()

        digests = _verify_pulled_digests(
            _FakeOras({"nested/extra.txt": want}),
            "imgrepo.damit.hu/supernova/iris-osl:v3",
            slug_dir,
            "repo://iris-osl:v3",
        )
        assert digests == {"nested/extra.txt": want}

    def test_verify_pulled_digests_flat_unchanged(self, _isolated_env: Path):
        # A flat artifact keeps the same {filename: digest} mapping as before.
        from solar_host.models_manager import _verify_pulled_digests

        slug_dir = self._artifact_dir(_isolated_env)
        data = b"x" * 1024
        (slug_dir / "model.gguf").write_bytes(data)
        want = hashlib.sha256(data).hexdigest()

        digests = _verify_pulled_digests(
            _FakeOras({"model.gguf": want}),
            "imgrepo.damit.hu/supernova/iris-osl:v3",
            slug_dir,
            "repo://iris-osl:v3",
        )
        assert digests == {"model.gguf": want}

    def test_verify_pulled_digests_detects_nested_mismatch(self, _isolated_env: Path):
        from solar_host.models_manager import ModelPullError, _verify_pulled_digests

        slug_dir = self._artifact_dir(_isolated_env)
        (slug_dir / "nested").mkdir()
        (slug_dir / "nested" / "extra.txt").write_bytes(b"y" * 1024)  # tampered
        want = hashlib.sha256(b"x" * 1024).hexdigest()

        with pytest.raises(ModelPullError) as ei:
            _verify_pulled_digests(
                _FakeOras({"nested/extra.txt": want}),
                "imgrepo.damit.hu/supernova/iris-osl:v3",
                slug_dir,
                "repo://iris-osl:v3",
            )
        assert "nested/extra.txt" in str(ei.value)

    def test_verify_pulled_digests_detects_missing_nested_file(
        self, _isolated_env: Path
    ):
        from solar_host.models_manager import ModelPullError, _verify_pulled_digests

        slug_dir = self._artifact_dir(_isolated_env)  # no nested/ directory
        want = hashlib.sha256(b"x" * 1024).hexdigest()

        with pytest.raises(ModelPullError) as ei:
            _verify_pulled_digests(
                _FakeOras({"nested/extra.txt": want}),
                "imgrepo.damit.hu/supernova/iris-osl:v3",
                slug_dir,
                "repo://iris-osl:v3",
            )
        assert "nested/extra.txt: missing on disk" in str(ei.value)

    def test_verify_pulled_digests_rejects_traversal_title(self, _isolated_env: Path):
        from solar_host.models_manager import ModelPullError, _verify_pulled_digests

        slug_dir = self._artifact_dir(_isolated_env)
        (slug_dir / "model.gguf").write_bytes(b"x" * 1024)

        with pytest.raises(ModelPullError) as ei:
            _verify_pulled_digests(
                _FakeOras({"../escape": "abc"}),
                "imgrepo.damit.hu/supernova/iris-osl:v3",
                slug_dir,
                "repo://iris-osl:v3",
            )
        assert ".." in str(ei.value)

    def test_verify_cached_digests_nested(self, _isolated_env: Path):
        from solar_host.models_manager import _verify_cached_digests

        slug_dir = self._artifact_dir(_isolated_env)
        (slug_dir / "nested").mkdir()
        data = b"x" * 1024
        (slug_dir / "nested" / "extra.txt").write_bytes(data)
        entry = _make_manifest_entry(path=str(slug_dir.resolve()))
        entry.file_digests = {"nested/extra.txt": hashlib.sha256(data).hexdigest()}

        assert _verify_cached_digests(entry) is True

    def test_cache_hit_with_matching_digests_returns_cached(
        self, client: TestClient, _isolated_env: Path
    ):
        ensure_models_dir()
        slug_dir = self._artifact_dir(_isolated_env)
        data = b"x" * 1024
        (slug_dir / "model.gguf").write_bytes(data)
        entry = _make_manifest_entry(path=str(slug_dir.resolve()))
        entry.file_digests = {"model.gguf": hashlib.sha256(data).hexdigest()}
        add_manifest_entry(entry)

        with patch("solar_host.models_manager._pull_harbor") as mock_pull:
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["cached"] is True
        mock_pull.assert_not_called()

    def test_cache_hit_with_corrupt_file_re_pulls(
        self, client: TestClient, _isolated_env: Path
    ):
        """Corrupt cached artifact -> digest check fires -> re-pull."""
        ensure_models_dir()
        slug_dir = self._artifact_dir(_isolated_env)
        (slug_dir / "model.gguf").write_bytes(b"corrupt!")
        entry = _make_manifest_entry(path=str(slug_dir.resolve()))
        entry.file_digests = {"model.gguf": hashlib.sha256(b"original").hexdigest()}
        add_manifest_entry(entry)

        def _recreate(harbor_ref, target_dir, source_uri):
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "model.gguf").write_bytes(b"x" * 1024)

        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=_recreate,
        ) as mock_pull:
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["cached"] is False
        mock_pull.assert_called_once()

    def test_cache_hit_without_digests_still_cached(
        self, client: TestClient, _isolated_env: Path
    ):
        """Pre-D-017 entries (no file_digests) keep the legacy cache behavior."""
        ensure_models_dir()
        slug_dir = self._artifact_dir(_isolated_env)
        (slug_dir / "model.gguf").write_bytes(b"x" * 1024)
        add_manifest_entry(_make_manifest_entry(path=str(slug_dir.resolve())))

        with patch("solar_host.models_manager._pull_harbor") as mock_pull:
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["cached"] is True
        mock_pull.assert_not_called()

    def test_pull_records_file_digests_in_manifest(
        self, client: TestClient, _isolated_env: Path
    ):
        def _pull_with_digests(harbor_ref, target_dir, source_uri):
            target_dir.mkdir(parents=True, exist_ok=True)
            data = b"x" * 1024
            (target_dir / "model.gguf").write_bytes(data)
            return {"model.gguf": hashlib.sha256(data).hexdigest()}

        with patch(
            "solar_host.models_manager._pull_harbor", side_effect=_pull_with_digests
        ):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 200

        entry = get_manifest_entry("repo://iris-osl:v3")
        assert entry is not None
        assert entry.file_digests == {
            "model.gguf": hashlib.sha256(b"x" * 1024).hexdigest()
        }


# ---------------------------------------------------------------------------
# Harbor pull (cache miss)
# ---------------------------------------------------------------------------


class TestHarborPull:
    def _make_mock_pull(self, tmp_path: Path):
        """Return a side_effect function that creates a dummy file in target_dir."""

        def _side_effect(harbor_ref: str, target_dir: Path, source_uri: str):
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "model.gguf").write_bytes(b"x" * 1024)

        return _side_effect

    def test_harbor_pull_cache_miss(self, client: TestClient, _isolated_env: Path):
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._make_mock_pull(_isolated_env),
        ) as mock_pull:
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is False
        assert data["source_uri"] == "repo://iris-osl:v3"
        assert "repo--iris-osl--v3" in data["path"]
        mock_pull.assert_called_once()

    def test_harbor_pull_updates_manifest(
        self, client: TestClient, _isolated_env: Path
    ):
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._make_mock_pull(_isolated_env),
        ):
            client.post("/models/pull", json=_harbor_body(), headers=_headers())

        entry = get_manifest_entry("repo://iris-osl:v3")
        assert entry is not None
        assert entry.slug == "repo--iris-osl--v3"
        assert entry.digest == "sha256:abc123"
        assert entry.size_bytes == 1024

    def test_harbor_pull_called_with_correct_args(
        self, client: TestClient, _isolated_env: Path
    ):
        captured = {}

        def _capture(harbor_ref, target_dir, source_uri):
            captured["harbor_ref"] = harbor_ref
            captured["target_dir"] = target_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "model.gguf").write_bytes(b"x")

        with patch("solar_host.models_manager._pull_harbor", side_effect=_capture):
            client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert captured["harbor_ref"] == "imgrepo.damit.hu/supernova/iris-osl:v3"
        assert str(captured["target_dir"]).endswith("repo--iris-osl--v3")

    def test_harbor_pull_with_subpath_returns_file_path(
        self, client: TestClient, _isolated_env: Path
    ):
        """Fresh pull for repo://name:version/model.gguf returns dir/model.gguf."""
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._make_mock_pull(_isolated_env),
        ) as mock_pull:
            body = _harbor_body(source_uri="repo://iris-osl:v3/model.gguf")
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is False
        assert data["path"].endswith("repo--iris-osl--v3/model.gguf")
        mock_pull.assert_called_once()

    def test_harbor_pull_with_missing_subpath_returns_404(
        self, client: TestClient, _isolated_env: Path
    ):
        """Subpath not present in the artifact → 404 and directory cleaned up."""
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._make_mock_pull(_isolated_env),
        ):
            body = _harbor_body(source_uri="repo://iris-osl:v3/nonexistent.gguf")
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 404
        data = resp.json()
        assert data["error"] == "not_found"
        assert "nonexistent.gguf" in data["detail"]

        # The stale directory must have been removed; manifest must be empty.
        slug_dir = _isolated_env / "repo--iris-osl--v3"
        assert not slug_dir.exists()
        assert get_manifest_entry("repo://iris-osl:v3/nonexistent.gguf") is None

    def test_second_pull_returns_cached(self, client: TestClient, _isolated_env: Path):
        """After a successful pull, the same URI returns cached=True."""
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._make_mock_pull(_isolated_env),
        ):
            resp1 = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp1.json()["cached"] is False

        with patch("solar_host.models_manager._pull_harbor") as mock_pull:
            resp2 = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp2.json()["cached"] is True
        mock_pull.assert_not_called()

    def test_harbor_pull_persists_repo_metadata(
        self, client: TestClient, _isolated_env: Path
    ):
        """D-016: Data Repository metadata fields round-trip into the manifest."""
        body = {
            **_harbor_body(),
            "category": "model",
            "name": "iris-osl",
            "version": "v3",
            "checksum": "sha256:abc123",
            "metadata": {"format": "gguf", "quantization": "Q4_K_M"},
        }
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._make_mock_pull(_isolated_env),
        ):
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        entry = get_manifest_entry("repo://iris-osl:v3")
        assert entry is not None
        assert entry.category == "model"
        assert entry.name == "iris-osl"
        assert entry.version == "v3"
        assert entry.checksum == "sha256:abc123"
        assert entry.metadata == {"format": "gguf", "quantization": "Q4_K_M"}

    def test_repo_metadata_surfaced_in_get_models(
        self, client: TestClient, _isolated_env: Path
    ):
        """GET /models returns the Data Repository metadata for D-016 pulls."""
        body = {
            **_harbor_body(),
            "category": "model",
            "name": "iris-osl",
            "version": "v3",
            "metadata": {"format": "gguf"},
        }
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._make_mock_pull(_isolated_env),
        ):
            client.post("/models/pull", json=body, headers=_headers())

        resp = client.get("/models", headers=_headers())
        assert resp.status_code == 200
        entries = [m for m in resp.json() if m["name"] == "repo--iris-osl--v3"]
        assert len(entries) == 1
        m = entries[0]
        assert m["category"] == "model"
        assert m["model_name"] == "iris-osl"
        assert m["version"] == "v3"
        assert m["metadata"] == {"format": "gguf"}

    def test_harbor_pull_without_repo_metadata_still_works(
        self, client: TestClient, _isolated_env: Path
    ):
        """Pulls that omit the D-016 fields are still accepted (back-compat)."""
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=self._make_mock_pull(_isolated_env),
        ):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 200
        entry = get_manifest_entry("repo://iris-osl:v3")
        assert entry is not None
        assert entry.category is None
        assert entry.name is None
        assert entry.version is None
        assert entry.metadata is None


# ---------------------------------------------------------------------------
# HuggingFace pull (cache miss)
# ---------------------------------------------------------------------------


class TestHuggingFacePull:
    def _make_mock_dl(self, _isolated_env: Path):
        def _side_effect(model_id, target_dir, source_uri, allow_patterns=None):
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "pytorch_model.bin").write_bytes(b"w" * 2048)

        return _side_effect

    def test_hf_pull_cache_miss(self, client: TestClient, _isolated_env: Path):
        with patch(
            "solar_host.models_manager._pull_huggingface",
            side_effect=self._make_mock_dl(_isolated_env),
        ):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is False
        assert "hf--microsoft--phi-3" in data["path"]

    def test_hf_pull_updates_manifest(self, client: TestClient, _isolated_env: Path):
        with patch(
            "solar_host.models_manager._pull_huggingface",
            side_effect=self._make_mock_dl(_isolated_env),
        ):
            client.post("/models/pull", json=_hf_body(), headers=_headers())

        entry = get_manifest_entry("huggingface://microsoft/phi-3")
        assert entry is not None
        assert entry.slug == "hf--microsoft--phi-3"
        assert entry.size_bytes == 2048

    def test_hf_pull_passes_none_token_when_empty(
        self, client: TestClient, _isolated_env: Path
    ):
        """When hf_token is empty string, snapshot_download must receive token=None."""
        captured = {}

        def _capture(model_id, target_dir, source_uri, allow_patterns=None):
            captured["model_id"] = model_id
            captured["target_dir"] = target_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "model.bin").write_bytes(b"x")

        with patch("solar_host.models_manager._pull_huggingface", side_effect=_capture):
            client.post("/models/pull", json=_hf_body(), headers=_headers())

        assert captured["model_id"] == "microsoft/phi-3"

    def test_hf_pull_with_token(
        self, client: TestClient, _isolated_env: Path, monkeypatch
    ):
        monkeypatch.setattr("solar_host.config.settings.hf_token", "hf_mytoken123")
        captured_token = {}

        def _fake_snapshot(repo_id, local_dir, token, allow_patterns=None):
            captured_token["token"] = token
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / "model.bin").write_bytes(b"x")

        with patch("huggingface_hub.snapshot_download", side_effect=_fake_snapshot):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())

        assert resp.status_code == 200
        assert captured_token["token"] == "hf_mytoken123"

    def test_hf_pull_passes_none_not_empty_string(
        self, client: TestClient, _isolated_env: Path
    ):
        """Empty hf_token must become None, not empty string, in snapshot_download."""
        captured_token = {}

        def _fake_snapshot(repo_id, local_dir, token, allow_patterns=None):
            captured_token["token"] = token
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / "model.bin").write_bytes(b"x")

        with patch("huggingface_hub.snapshot_download", side_effect=_fake_snapshot):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())

        assert resp.status_code == 200
        assert captured_token["token"] is None


# ---------------------------------------------------------------------------
# HuggingFace download filters
# ---------------------------------------------------------------------------


class TestFileFilters:
    """A filtered snapshot downloads only the requested files."""

    def _capturing_dl(self, calls: list, files: dict[str, bytes] | None = None):
        def _side_effect(model_id, target_dir, source_uri, allow_patterns=None):
            calls.append(allow_patterns)
            target_dir.mkdir(parents=True, exist_ok=True)
            for name, data in (files or {"model.gguf": b"x" * 32}).items():
                file = target_dir / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)

        return _side_effect

    def _seed_cache(self, models_dir: Path, file_filters: list[str] | None) -> Path:
        ensure_models_dir()
        slug_dir = models_dir / "hf--microsoft--phi-3"
        slug_dir.mkdir(parents=True, exist_ok=True)
        (slug_dir / "model.gguf").write_bytes(b"x" * 32)
        add_manifest_entry(
            _make_manifest_entry(
                slug="hf--microsoft--phi-3",
                source_uri="huggingface://microsoft/phi-3",
                path=str(slug_dir.resolve()),
                file_filters=file_filters,
            )
        )
        return slug_dir

    def test_filters_reach_snapshot_download(
        self, client: TestClient, _isolated_env: Path
    ):
        captured = {}

        def _fake_snapshot(repo_id, local_dir, token, allow_patterns=None):
            captured["allow_patterns"] = allow_patterns
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / "model.gguf").write_bytes(b"x")

        with patch("huggingface_hub.snapshot_download", side_effect=_fake_snapshot):
            body = _hf_body(file_filters=["*UD-Q4_K_XL*", "mmproj-BF16.gguf"])
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        assert captured["allow_patterns"] == ["*UD-Q4_K_XL*", "mmproj-BF16.gguf"]

    def test_filters_recorded_on_manifest(
        self, client: TestClient, _isolated_env: Path
    ):
        with patch(
            "solar_host.models_manager._pull_huggingface",
            side_effect=self._capturing_dl([]),
        ):
            body = _hf_body(file_filters=["*UD-Q4_K_XL*"])
            client.post("/models/pull", json=body, headers=_headers())

        entry = get_manifest_entry("huggingface://microsoft/phi-3")
        assert entry is not None
        assert entry.file_filters == ["*UD-Q4_K_XL*"]

    def test_no_filters_means_no_allow_patterns(
        self, client: TestClient, _isolated_env: Path
    ):
        calls: list = []
        with patch(
            "solar_host.models_manager._pull_huggingface",
            side_effect=self._capturing_dl(calls),
        ):
            client.post("/models/pull", json=_hf_body(), headers=_headers())

        assert calls == [None]
        entry = get_manifest_entry("huggingface://microsoft/phi-3")
        assert entry is not None and entry.file_filters is None

    def test_subset_request_reuses_cached_snapshot(
        self, client: TestClient, _isolated_env: Path
    ):
        self._seed_cache(_isolated_env, ["*UD-Q4_K_XL*", "mmproj-BF16.gguf"])
        with patch("solar_host.models_manager._pull_huggingface") as mock_dl:
            body = _hf_body(file_filters=["*UD-Q4_K_XL*"])
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["cached"] is True
        mock_dl.assert_not_called()

    def test_full_cached_snapshot_satisfies_any_filter(
        self, client: TestClient, _isolated_env: Path
    ):
        self._seed_cache(_isolated_env, None)
        with patch("solar_host.models_manager._pull_huggingface") as mock_dl:
            body = _hf_body(file_filters=["*UD-Q4_K_XL*"])
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        assert resp.json()["cached"] is True
        mock_dl.assert_not_called()

    def test_new_pattern_tops_up_cached_snapshot_with_union(
        self, client: TestClient, _isolated_env: Path
    ):
        slug_dir = self._seed_cache(_isolated_env, ["*UD-Q4_K_XL*"])
        calls: list = []
        with patch(
            "solar_host.models_manager._pull_huggingface",
            side_effect=self._capturing_dl(calls),
        ):
            body = _hf_body(file_filters=["mmproj-BF16.gguf"])
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        assert calls == [["*UD-Q4_K_XL*", "mmproj-BF16.gguf"]]
        # The already-downloaded files survive the top-up.
        assert (slug_dir / "model.gguf").exists()
        entry = get_manifest_entry("huggingface://microsoft/phi-3")
        assert entry is not None
        assert entry.file_filters == ["*UD-Q4_K_XL*", "mmproj-BF16.gguf"]

    def test_unfiltered_request_redownloads_filtered_snapshot(
        self, client: TestClient, _isolated_env: Path
    ):
        self._seed_cache(_isolated_env, ["*UD-Q4_K_XL*"])
        calls: list = []
        with patch(
            "solar_host.models_manager._pull_huggingface",
            side_effect=self._capturing_dl(calls),
        ):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())

        assert resp.status_code == 200
        assert calls == [None]
        entry = get_manifest_entry("huggingface://microsoft/phi-3")
        assert entry is not None and entry.file_filters is None

    def test_filters_ignored_for_harbor_pull(
        self, client: TestClient, _isolated_env: Path
    ):
        calls: list = []

        def _side_effect(harbor_ref, target_dir, source_uri, allow_patterns=None):
            calls.append(allow_patterns)
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "model.gguf").write_bytes(b"x" * 32)

        with patch("solar_host.models_manager._pull_harbor", side_effect=_side_effect):
            body = _harbor_body(file_filters=["*UD-Q4_K_XL*"])
            resp = client.post("/models/pull", json=body, headers=_headers())

        assert resp.status_code == 200
        entry = get_manifest_entry("repo://iris-osl:v3")
        assert entry is not None and entry.file_filters is None


# ---------------------------------------------------------------------------
# Missing credentials (500)
# ---------------------------------------------------------------------------


class TestMissingCredentials:
    def test_missing_harbor_url_returns_500(self, client: TestClient, monkeypatch):
        monkeypatch.setattr("solar_host.config.settings.harbor_url", "")
        resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 500
        data = resp.json()
        assert data["error"] == "credentials_missing"

    def test_missing_harbor_username_returns_500(self, client: TestClient, monkeypatch):
        monkeypatch.setattr("solar_host.config.settings.harbor_username", "")
        resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 500
        data = resp.json()
        assert data["error"] == "credentials_missing"

    def test_missing_harbor_password_returns_500(self, client: TestClient, monkeypatch):
        monkeypatch.setattr("solar_host.config.settings.harbor_password", "")
        resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 500
        data = resp.json()
        assert data["error"] == "credentials_missing"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    """Verify that library exceptions map to the correct HTTP status codes
    and that the response body matches the spec Section 6.2 format."""

    def _post_harbor(self, client, exc_to_raise):
        with patch(
            "solar_host.models_manager._pull_harbor",
            side_effect=exc_to_raise,
        ):
            return client.post("/models/pull", json=_harbor_body(), headers=_headers())

    def _assert_error_body(self, resp, expected_status: int, expected_error: str):
        assert resp.status_code == expected_status
        data = resp.json()
        assert data["error"] == expected_error
        assert "detail" in data
        assert data["source_uri"] == "repo://iris-osl:v3"
        assert data["status_code"] == expected_status

    def test_harbor_connection_error_returns_502(self, client: TestClient):
        from solar_host.models_manager import ModelPullError

        exc = ModelPullError(
            502, "source_unreachable", "Harbor unreachable", "repo://iris-osl:v3"
        )
        resp = self._post_harbor(client, exc)
        self._assert_error_body(resp, 502, "source_unreachable")

    def test_harbor_auth_error_returns_401(self, client: TestClient):
        from solar_host.models_manager import ModelPullError

        exc = ModelPullError(401, "auth_failed", "Auth failed", "repo://iris-osl:v3")
        resp = self._post_harbor(client, exc)
        self._assert_error_body(resp, 401, "auth_failed")

    def test_artifact_not_found_returns_404(self, client: TestClient):
        from solar_host.models_manager import ModelPullError

        exc = ModelPullError(404, "not_found", "Not found", "repo://iris-osl:v3")
        resp = self._post_harbor(client, exc)
        self._assert_error_body(resp, 404, "not_found")

    def test_disk_full_returns_507(self, client: TestClient):
        from solar_host.models_manager import ModelPullError

        exc = ModelPullError(
            507, "insufficient_storage", "Disk full", "repo://iris-osl:v3"
        )
        resp = self._post_harbor(client, exc)
        self._assert_error_body(resp, 507, "insufficient_storage")

    def test_hf_repo_not_found_returns_404(self, client: TestClient):
        from solar_host.models_manager import ModelPullError

        exc = ModelPullError(
            404, "not_found", "HF repo not found", "huggingface://microsoft/phi-3"
        )
        with patch("solar_host.models_manager._pull_huggingface", side_effect=exc):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_hf_gated_repo_returns_401(self, client: TestClient):
        from solar_host.models_manager import ModelPullError

        exc = ModelPullError(
            401, "auth_failed", "Gated repo", "huggingface://microsoft/phi-3"
        )
        with patch("solar_host.models_manager._pull_huggingface", side_effect=exc):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())
        assert resp.status_code == 401
        assert resp.json()["error"] == "auth_failed"


# ---------------------------------------------------------------------------
# Failure cleanup
# ---------------------------------------------------------------------------


class TestFailureCleanup:
    def test_partial_directory_cleaned_up_on_failure(
        self, client: TestClient, _isolated_env: Path
    ):
        """If download raises, the (possibly partial) target directory must not remain."""
        from solar_host.models_manager import ModelPullError

        slug_dir = _isolated_env / "repo--iris-osl--v3"

        def _fail(harbor_ref, target_dir, source_uri):
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "partial.bin").write_bytes(b"partial")
            raise ModelPullError(502, "source_unreachable", "Harbor down", source_uri)

        with patch("solar_host.models_manager._pull_harbor", side_effect=_fail):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 502
        assert not slug_dir.exists(), "Partial directory should have been cleaned up"

    def test_failed_pull_not_added_to_manifest(
        self, client: TestClient, _isolated_env: Path
    ):
        from solar_host.models_manager import ModelPullError

        def _fail(harbor_ref, target_dir, source_uri):
            raise ModelPullError(404, "not_found", "Not found", source_uri)

        with patch("solar_host.models_manager._pull_harbor", side_effect=_fail):
            client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert get_manifest_entry("repo://iris-osl:v3") is None

    def test_stale_directory_removed_before_pull(
        self, client: TestClient, _isolated_env: Path
    ):
        """Pre-existing orphan directory is deleted before a fresh download starts."""
        ensure_models_dir()
        slug_dir = get_models_dir() / "repo--iris-osl--v3"
        slug_dir.mkdir(parents=True, exist_ok=True)
        stale_file = slug_dir / "stale.bin"
        stale_file.write_bytes(b"stale data")

        removed_before_dl = {}

        def _check_and_create(harbor_ref, target_dir, source_uri):
            removed_before_dl["stale_gone"] = not stale_file.exists()
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "fresh.bin").write_bytes(b"fresh data")

        with patch(
            "solar_host.models_manager._pull_harbor", side_effect=_check_and_create
        ):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 200
        assert (
            removed_before_dl.get("stale_gone") is True
        ), "Stale directory should have been removed before download started"


# ---------------------------------------------------------------------------
# OSError / ENOSPC
# ---------------------------------------------------------------------------


class TestDiskFull:
    def test_enospc_during_harbor_pull_returns_507(
        self, client: TestClient, _isolated_env: Path
    ):
        """An OSError with errno.ENOSPC from the download must surface as 507."""
        import errno as _errno

        def _fail(harbor_ref, target_dir, source_uri):
            target_dir.mkdir(parents=True, exist_ok=True)
            raise OSError(_errno.ENOSPC, "No space left on device")

        with patch("solar_host.models_manager._pull_harbor", side_effect=_fail):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 507
        data = resp.json()
        assert data["error"] == "insufficient_storage"
        assert data["source_uri"] == "repo://iris-osl:v3"

    def test_enospc_during_hf_pull_returns_507(
        self, client: TestClient, _isolated_env: Path
    ):
        import errno as _errno

        def _fail(model_id, target_dir, source_uri, allow_patterns=None):
            target_dir.mkdir(parents=True, exist_ok=True)
            raise OSError(_errno.ENOSPC, "No space left on device")

        with patch("solar_host.models_manager._pull_huggingface", side_effect=_fail):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())

        assert resp.status_code == 507
        assert resp.json()["error"] == "insufficient_storage"

    def test_non_enospc_oserror_returns_500(
        self, client: TestClient, _isolated_env: Path
    ):
        import errno as _errno

        def _fail(harbor_ref, target_dir, source_uri):
            target_dir.mkdir(parents=True, exist_ok=True)
            raise OSError(_errno.EACCES, "Permission denied")

        with patch("solar_host.models_manager._pull_harbor", side_effect=_fail):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 500
        assert resp.json()["error"] == "model_pull_failed"


# ---------------------------------------------------------------------------
# _map_download_exception integration
# ---------------------------------------------------------------------------


class TestMapDownloadException:
    """Exercise _map_download_exception via pull_model() with real-shaped exceptions."""

    @staticmethod
    def _make_exc(module: str, name: str, msg: str = "boom") -> Exception:
        """Create an exception whose __module__ and __name__ match a library."""
        cls = type(name, (Exception,), {"__module__": module})
        return cls(msg)

    def test_harbor_connection_error_mapped(
        self, client: TestClient, _isolated_env: Path
    ):
        exc = self._make_exc("harbor_oci_client.exceptions", "HarborConnectionError")
        with patch("solar_host.models_manager._pull_harbor", side_effect=exc):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 502
        assert resp.json()["error"] == "source_unreachable"

    def test_harbor_auth_error_mapped(self, client: TestClient, _isolated_env: Path):
        exc = self._make_exc("harbor_oci_client.exceptions", "HarborAuthError")
        with patch("solar_host.models_manager._pull_harbor", side_effect=exc):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 401
        assert resp.json()["error"] == "auth_failed"

    def test_artifact_not_found_mapped(self, client: TestClient, _isolated_env: Path):
        exc = self._make_exc("harbor_oci_client.exceptions", "ArtifactNotFoundError")
        with patch("solar_host.models_manager._pull_harbor", side_effect=exc):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_unknown_harbor_error_mapped_to_502(
        self, client: TestClient, _isolated_env: Path
    ):
        exc = self._make_exc("harbor_oci_client.exceptions", "HarborAPIError")
        with patch("solar_host.models_manager._pull_harbor", side_effect=exc):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 502
        assert resp.json()["error"] == "source_unreachable"

    def test_hf_repository_not_found_mapped(
        self, client: TestClient, _isolated_env: Path
    ):
        exc = self._make_exc("huggingface_hub.utils", "RepositoryNotFoundError")
        with patch("solar_host.models_manager._pull_huggingface", side_effect=exc):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_hf_real_repository_not_found_not_swallowed_as_plain_oserror(
        self, client: TestClient, _isolated_env: Path
    ):
        """RepositoryNotFoundError subclasses OSError (via httpx); pull must return 404."""
        import httpx
        from huggingface_hub.errors import RepositoryNotFoundError

        req = httpx.Request(
            "GET", "https://huggingface.co/api/models/microsoft/phi-3/revision/main"
        )
        hf_exc = RepositoryNotFoundError(
            "Repository Not Found for url: https://huggingface.co/...",
            response=httpx.Response(401, request=req),
        )
        with patch("huggingface_hub.snapshot_download", side_effect=hf_exc):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_hf_gated_repo_mapped(self, client: TestClient, _isolated_env: Path):
        exc = self._make_exc("huggingface_hub.utils", "GatedRepoError")
        with patch("solar_host.models_manager._pull_huggingface", side_effect=exc):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())
        assert resp.status_code == 401
        assert resp.json()["error"] == "auth_failed"

    def test_unknown_hf_error_mapped_to_502(
        self, client: TestClient, _isolated_env: Path
    ):
        exc = self._make_exc("huggingface_hub.utils", "HfHubHTTPError")
        with patch("solar_host.models_manager._pull_huggingface", side_effect=exc):
            resp = client.post("/models/pull", json=_hf_body(), headers=_headers())
        assert resp.status_code == 502
        assert resp.json()["error"] == "source_unreachable"

    def test_unknown_exception_mapped_to_500(
        self, client: TestClient, _isolated_env: Path
    ):
        exc = RuntimeError("something unexpected")
        with patch("solar_host.models_manager._pull_harbor", side_effect=exc):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())
        assert resp.status_code == 500
        assert resp.json()["error"] == "model_pull_failed"


# ---------------------------------------------------------------------------
# Response structure
# ---------------------------------------------------------------------------


class TestResponseStructure:
    def test_response_contains_required_fields(
        self, client: TestClient, _isolated_env: Path
    ):
        def _create(harbor_ref, target_dir, source_uri):
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "model.gguf").write_bytes(b"x" * 512)

        with patch("solar_host.models_manager._pull_harbor", side_effect=_create):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"path", "cached", "source_uri"}

    def test_error_response_contains_required_fields(self, client: TestClient):
        from solar_host.models_manager import ModelPullError

        exc = ModelPullError(404, "not_found", "Gone", "repo://iris-osl:v3")
        with patch("solar_host.models_manager._pull_harbor", side_effect=exc):
            resp = client.post("/models/pull", json=_harbor_body(), headers=_headers())

        data = resp.json()
        assert set(data.keys()) == {"error", "detail", "source_uri", "status_code"}


# ---------------------------------------------------------------------------
# C4: pull progress telemetry (progress_cb)
# ---------------------------------------------------------------------------


class _FakePool:
    def __init__(self, future):
        self._future = future

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def schedule(self, fn, args):
        return self._future


class _FakeFuture:
    def __init__(self, polls: int = 6, result=None, error=None):
        self._polls = polls
        self._result = result
        self._error = error
        self.cancelled = False

    def done(self) -> bool:
        self._polls -= 1
        return self._polls <= 0

    def cancel(self) -> bool:
        self.cancelled = True
        return True

    def result(self):
        if self._error is not None:
            raise self._error
        return self._result


class TestPullProgress:
    def test_in_process_emits_start_and_terminal_events(self, _isolated_env: Path):
        from solar_host.models_manager import ensure_models_dir, pull_model

        ensure_models_dir()
        events: list[dict] = []
        with patch("solar_host.models_manager._pull_harbor", return_value=None):
            pull_model(
                source="harbor",
                source_uri="repo://iris-osl:v3",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
                size_bytes=500,
                progress_cb=events.append,
            )

        phases = [e["phase"] for e in events]
        assert phases == ["resolving", "verifying", "finalizing", "completed"]
        assert sum(1 for e in events if e["phase"] == "completed") == 1
        for e in events:
            assert e["source_uri"] == "repo://iris-osl:v3"
            assert e["bytes_total"] == 500

    def test_in_process_failure_emits_failed_exactly_once(self, _isolated_env: Path):
        from solar_host.models_manager import ModelPullError, pull_model

        events: list[dict] = []
        exc = ModelPullError(404, "not_found", "Gone", "repo://iris-osl:v3")
        with (
            patch("solar_host.models_manager._pull_harbor", side_effect=exc),
            pytest.raises(ModelPullError),
        ):
            pull_model(
                source="harbor",
                source_uri="repo://iris-osl:v3",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
                progress_cb=events.append,
            )

        phases = [e["phase"] for e in events]
        assert phases == ["resolving", "failed"]
        assert sum(1 for e in events if e["phase"] == "failed") == 1
        assert not any(e["phase"] == "completed" for e in events)

    def test_progress_cb_none_is_noop(self, _isolated_env: Path):
        from solar_host.models_manager import ensure_models_dir, pull_model

        ensure_models_dir()

        with patch("solar_host.models_manager._pull_harbor", return_value=None):
            result = pull_model(
                source="harbor",
                source_uri="repo://iris-osl:v3",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
            )
        assert result["cached"] is False

    def test_subprocess_poll_loop_emits_throttled_downloading(
        self, _isolated_env: Path, monkeypatch
    ):
        """The parent poll loop measures on-disk growth: bytes_done is
        monotonically non-decreasing, bytes_total comes from the declared
        size, and emission is throttled to pull_progress_interval_s."""
        from solar_host.models_manager import ensure_models_dir, pull_model

        ensure_models_dir()
        monkeypatch.setattr("solar_host.config.settings.pull_use_subprocess", True)
        monkeypatch.setattr("solar_host.config.settings.pull_progress_interval_s", 0.12)
        counter = {"n": 0}

        def _fake_size(path) -> int:
            counter["n"] += 1
            return counter["n"] * 100

        monkeypatch.setattr("solar_host.models_manager._compute_dir_size", _fake_size)
        events: list[dict] = []
        future = _FakeFuture(polls=6, result=None)
        with patch("pebble.ProcessPool", lambda max_workers: _FakePool(future)):
            pull_model(
                source="harbor",
                source_uri="repo://iris-osl:v3",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
                size_bytes=1000,
                progress_cb=events.append,
            )

        downloading = [e for e in events if e["phase"] == "downloading"]
        assert downloading, "expected throttled downloading events"
        # Throttled: far fewer events than polls.
        assert len(downloading) < 6
        done_values = [e["bytes_done"] for e in downloading]
        assert done_values == sorted(done_values)
        assert done_values[0] >= 100
        assert all(e["bytes_total"] == 1000 for e in downloading)
        for _prev, cur in itertools.pairwise(downloading):
            assert cur["speed_bps"] is not None and cur["speed_bps"] >= 0
        assert events[-1]["phase"] == "completed"

    def test_subprocess_failure_emits_failed_once(
        self, _isolated_env: Path, monkeypatch
    ):
        from solar_host.models_manager import (
            ModelPullError,
            ensure_models_dir,
            pull_model,
        )

        ensure_models_dir()
        monkeypatch.setattr("solar_host.config.settings.pull_use_subprocess", True)
        events: list[dict] = []
        exc = ModelPullError(500, "model_pull_failed", "boom", "repo://iris-osl:v3")
        future = _FakeFuture(polls=2, result=None, error=exc)
        with (
            patch("pebble.ProcessPool", lambda max_workers: _FakePool(future)),
            pytest.raises(ModelPullError),
        ):
            pull_model(
                source="harbor",
                source_uri="repo://iris-osl:v3",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
                progress_cb=events.append,
            )

        assert sum(1 for e in events if e["phase"] == "failed") == 1
        assert not any(e["phase"] == "completed" for e in events)

    def test_low_disk_abort_emits_failed_exactly_once(
        self, _isolated_env: Path, monkeypatch
    ):
        """The abort emits 'failed' and then raises; the handler emits it again.

        Two terminal events for one pull make consumers keyed on "first
        terminal wins" disagree with those keyed on "last wins".
        """
        from solar_host.models_manager import (
            ModelPullError,
            ensure_models_dir,
            pull_model,
        )

        ensure_models_dir()
        monkeypatch.setattr("solar_host.config.settings.pull_use_subprocess", True)
        monkeypatch.setattr("solar_host.config.settings.min_free_disk_gb", 10_000.0)
        events: list[dict] = []
        future = _FakeFuture(polls=50)
        with (
            patch("pebble.ProcessPool", lambda max_workers: _FakePool(future)),
            pytest.raises(ModelPullError) as excinfo,
        ):
            pull_model(
                source="harbor",
                source_uri="repo://iris-osl:v3",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
                # A declared size keeps the proactive check (step 3.5) happy so
                # the mid-download abort is the one that fires.
                size_bytes=500,
                progress_cb=events.append,
            )

        assert excinfo.value.status_code == 507
        assert "during download" in excinfo.value.detail
        assert sum(1 for e in events if e["phase"] == "failed") == 1

    def test_progress_cb_exception_does_not_abort_the_pull(self, _isolated_env: Path):
        """A bad telemetry consumer must not fail an otherwise fine download."""
        from solar_host.models_manager import ensure_models_dir, pull_model

        ensure_models_dir()
        calls: list[str] = []

        def _bad_cb(payload: dict) -> None:
            calls.append(payload["phase"])
            raise RuntimeError("consumer exploded")

        with patch("solar_host.models_manager._pull_harbor", return_value=None):
            result = pull_model(
                source="harbor",
                source_uri="repo://iris-osl:v3",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
                progress_cb=_bad_cb,
            )

        assert result is not None
        # Every phase was still attempted, including the terminal one.
        assert "completed" in calls
