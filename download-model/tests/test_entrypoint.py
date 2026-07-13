"""Tests for download-model/entrypoint.py.

All external calls (requests.get, OrasHelper.pull) are mocked.
Filesystem operations use tmp_path — nothing touches the real disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The entrypoint module lives in a directory named with a hyphen
# (download-model/), which is not a valid Python package name.
# Import it by adding the parent directory to sys.path.
_HERE = Path(__file__).resolve().parent
_MODEL_DIR = _HERE.parent  # download-model/
sys.path.insert(0, str(_MODEL_DIR))

import entrypoint  # noqa: E402

# ---------------------------------------------------------------------------
# Test: missing environment variables
# ---------------------------------------------------------------------------


class TestMissingEnvVars:
    def test_missing_model_uri(self, monkeypatch):
        monkeypatch.delenv("MODEL_URI", raising=False)
        monkeypatch.setenv("MODEL_OUTPUT_DIR", "/workspace/models/test")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with pytest.raises(entrypoint.MissingEnvError, match="MODEL_URI"):
            entrypoint.main()

    def test_missing_model_output_dir(self, monkeypatch):
        monkeypatch.setenv("MODEL_URI", "repo://test:v1")
        monkeypatch.delenv("MODEL_OUTPUT_DIR", raising=False)
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with pytest.raises(entrypoint.MissingEnvError, match="MODEL_OUTPUT_DIR"):
            entrypoint.main()

    def test_missing_data_repo_url(self, monkeypatch):
        monkeypatch.setenv("MODEL_URI", "repo://test:v1")
        monkeypatch.setenv("MODEL_OUTPUT_DIR", "/workspace/models/test")
        monkeypatch.delenv("DATA_REPOSITORY_URL", raising=False)
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with pytest.raises(entrypoint.MissingEnvError, match="DATA_REPOSITORY_URL"):
            entrypoint.main()

    def test_missing_harbor_url(self, monkeypatch):
        monkeypatch.setenv("MODEL_URI", "repo://test:v1")
        monkeypatch.setenv("MODEL_OUTPUT_DIR", "/workspace/models/test")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.delenv("HARBOR_URL", raising=False)
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with pytest.raises(entrypoint.MissingEnvError, match="HARBOR_URL"):
            entrypoint.main()

    def test_missing_harbor_credentials(self, monkeypatch):
        monkeypatch.setenv("MODEL_URI", "repo://test:v1")
        monkeypatch.setenv("MODEL_OUTPUT_DIR", "/workspace/models/test")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.delenv("HARBOR_USERNAME", raising=False)
        monkeypatch.delenv("HARBOR_PASSWORD", raising=False)

        with pytest.raises(entrypoint.MissingEnvError, match="HARBOR_USERNAME"):
            entrypoint.main()

    def test_missing_harbor_password(self, monkeypatch):
        monkeypatch.setenv("MODEL_URI", "repo://test:v1")
        monkeypatch.setenv("MODEL_OUTPUT_DIR", "/workspace/models/test")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.delenv("HARBOR_PASSWORD", raising=False)

        with pytest.raises(entrypoint.MissingEnvError, match="HARBOR_PASSWORD"):
            entrypoint.main()

    def test_cli_returns_nonzero_for_missing_model_uri(self):
        env = os.environ.copy()
        for variable in (
            "MODEL_URI",
            "MODEL_OUTPUT_DIR",
            "DATA_REPOSITORY_URL",
            "HARBOR_URL",
            "HARBOR_USERNAME",
            "HARBOR_PASSWORD",
        ):
            env.pop(variable, None)
        env["PYTHONUNBUFFERED"] = "1"

        result = subprocess.run(
            [sys.executable, str(_MODEL_DIR / "entrypoint.py")],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

        assert result.returncode == 1
        assert "MODEL_URI is required" in result.stdout


# ---------------------------------------------------------------------------
# Test: path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    def test_valid_path_under_workspace_models(self, tmp_path, monkeypatch):
        models_dir = tmp_path / "workspace" / "models"
        models_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_MODELS", models_dir)
        output_dir = models_dir / "my-model"
        output_dir.mkdir()

        result = entrypoint.validate_output_path(output_dir)
        assert result == output_dir.resolve()

    def test_path_outside_workspace_rejected(self, tmp_path, monkeypatch):
        models_dir = tmp_path / "workspace" / "models"
        models_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_MODELS", models_dir)
        bad_path = tmp_path / "not-workspace" / "models" / "my-model"
        bad_path.mkdir(parents=True)

        with pytest.raises(entrypoint.PathValidationError, match="must be under"):
            entrypoint.validate_output_path(bad_path)

    def test_path_escape_with_dotdot_rejected(self, tmp_path, monkeypatch):
        models_dir = tmp_path / "workspace" / "models"
        models_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_MODELS", models_dir)
        nested = tmp_path / "workspace" / "models" / ".." / ".." / "escape"
        nested.parent.mkdir(parents=True, exist_ok=True)

        with pytest.raises(entrypoint.PathValidationError, match="must be under"):
            entrypoint.validate_output_path(nested)

    def test_main_rejects_path_outside_workspace(self, monkeypatch):
        monkeypatch.setenv("MODEL_URI", "repo://test:v1")
        monkeypatch.setenv("MODEL_OUTPUT_DIR", "/tmp/not-a-workspace-model")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "password")

        with pytest.raises(entrypoint.PathValidationError, match="must be under"):
            entrypoint.main()


# ---------------------------------------------------------------------------
# Test: Data Repository resolution
# ---------------------------------------------------------------------------


class TestResolveModelUri:
    def test_successful_resolve(self):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {
                "harbor_ref": "harbor.example.com/project/model:v1",
                "checksum": "sha256:abc123",
                "size_bytes": 1024,
                "name": "model",
                "version": "v1",
                "category": "model",
            }
            mock_get.return_value = mock_resp

            result = entrypoint.resolve_model_uri("repo://model:v1", "http://repo:8000")

        assert result["harbor_ref"] == "harbor.example.com/project/model:v1"
        assert result["digest"] == "sha256:abc123"
        assert result["size_bytes"] == 1024
        mock_get.assert_called_once_with(
            "http://repo:8000/api/resolve",
            params={"uri": "repo://model:v1"},
            timeout=30,
        )

    def test_404_raises_resolve_error(self):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = False
            mock_resp.status_code = 404
            mock_resp.text = "Not Found"
            mock_get.return_value = mock_resp

            with pytest.raises(entrypoint.ResolveError, match="not found"):
                entrypoint.resolve_model_uri("repo://bad:v1", "http://repo:8000")

    def test_non_json_response_raises(self):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.side_effect = ValueError("not json")
            mock_resp.text = "plain text"
            mock_get.return_value = mock_resp

            with pytest.raises(entrypoint.ResolveError, match="non-JSON"):
                entrypoint.resolve_model_uri("repo://bad:v1", "http://repo:8000")

    def test_missing_harbor_ref_raises(self):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"name": "model", "version": "v1"}
            mock_get.return_value = mock_resp

            with pytest.raises(entrypoint.ResolveError, match="harbor_ref"):
                entrypoint.resolve_model_uri("repo://model:v1", "http://repo:8000")

    def test_connection_error_raises(self):
        import requests as req_mod

        with patch("requests.get", side_effect=req_mod.ConnectionError("timeout")):
            with pytest.raises(entrypoint.ResolveError, match="Failed to reach"):
                entrypoint.resolve_model_uri("repo://model:v1", "http://repo:8000")

    def test_server_error_raises_resolve_error(self):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock(ok=False, status_code=500, text="server error")
            mock_get.return_value = mock_resp

            with pytest.raises(entrypoint.ResolveError, match="returned 500"):
                entrypoint.resolve_model_uri("repo://model:v1", "http://repo:8000")

    @pytest.mark.parametrize(
        "response",
        [
            {"harbor_ref": "harbor.example.com/project/model:v1", "size_bytes": 1},
            {
                "harbor_ref": "harbor.example.com/project/model:v1",
                "checksum": "sha256:abc",
            },
            {
                "harbor_ref": "harbor.example.com/project/model:v1",
                "checksum": "sha256:abc",
                "size_bytes": -1,
            },
        ],
    )
    def test_missing_or_invalid_required_metadata_raises(self, response):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock(ok=True)
            mock_resp.json.return_value = response
            mock_get.return_value = mock_resp

            with pytest.raises(entrypoint.ResolveError, match="required|invalid"):
                entrypoint.resolve_model_uri("repo://model:v1", "http://repo:8000")


# ---------------------------------------------------------------------------
# Test: ORAS pull
# ---------------------------------------------------------------------------


class TestPullFromHarbor:
    def test_successful_pull(self, tmp_path):
        output = tmp_path / "model-dir"
        with patch("entrypoint.OrasHelper") as mock_oras_class:
            mock_oras = mock_oras_class.return_value
            mock_oras.pull.side_effect = lambda _ref, outdir: Path(
                outdir, "model.safetensors"
            ).write_bytes(b"model")

            entrypoint.pull_from_harbor(
                harbor_ref="harbor.example.com/project/model:v1",
                output_path=output,
                harbor_url="https://harbor.example.com",
                username="user",
                password="pass",
            )

        mock_oras_class.assert_called_once_with(
            hostname="harbor.example.com",
            username="user",
            password="pass",
        )
        pull_args, pull_kwargs = mock_oras.pull.call_args
        assert pull_args == ("harbor.example.com/project/model:v1",)
        assert Path(pull_kwargs["outdir"]).parent == output.parent
        assert Path(pull_kwargs["outdir"]).name.startswith(".model-dir.download-")
        assert output.exists()

    def test_preserves_non_default_harbor_port(self, tmp_path):
        output = tmp_path / "model-dir"
        with patch("entrypoint.OrasHelper") as mock_oras_class:
            mock_oras_class.return_value.pull.side_effect = lambda _ref, outdir: Path(
                outdir, "model.safetensors"
            ).write_bytes(b"model")

            entrypoint.pull_from_harbor(
                harbor_ref="harbor.example.com:5443/project/model:v1",
                output_path=output,
                harbor_url="https://harbor.example.com:5443",
                username="user",
                password="pass",
            )

        mock_oras_class.assert_called_once_with(
            hostname="harbor.example.com:5443",
            username="user",
            password="pass",
        )

    def test_harbor_error_raises_pull_error(self, tmp_path):
        output = tmp_path / "model-dir"
        from harbor_oci_client import HarborError

        with patch("entrypoint.OrasHelper") as mock_oras_class:
            mock_oras = mock_oras_class.return_value
            mock_oras.pull.side_effect = HarborError("auth failed")

            with pytest.raises(entrypoint.PullError, match="ORAS pull failed"):
                entrypoint.pull_from_harbor(
                    harbor_ref="harbor.example.com/project/bad:v1",
                    output_path=output,
                    harbor_url="https://harbor.example.com",
                    username="bad",
                    password="wrong",
                )

        assert not output.exists()

    def test_auth_error_during_client_initialization_raises_pull_error(self, tmp_path):
        output = tmp_path / "model-dir"
        from harbor_oci_client import HarborError

        with patch("entrypoint.OrasHelper", side_effect=HarborError("auth failed")):
            with pytest.raises(entrypoint.PullError, match="ORAS pull failed"):
                entrypoint.pull_from_harbor(
                    harbor_ref="harbor.example.com/project/bad:v1",
                    output_path=output,
                    harbor_url="https://harbor.example.com",
                    username="bad",
                    password="wrong",
                )

        assert not output.exists()

    def test_generic_oras_error_raises_pull_error(self, tmp_path):
        output = tmp_path / "model-dir"

        with patch("entrypoint.OrasHelper", side_effect=RuntimeError("network failed")):
            with pytest.raises(entrypoint.PullError, match="ORAS pull failed"):
                entrypoint.pull_from_harbor(
                    harbor_ref="harbor.example.com/project/bad:v1",
                    output_path=output,
                    harbor_url="https://harbor.example.com",
                    username="user",
                    password="pass",
                )

        assert not output.exists()

    def test_empty_pull_is_rejected_without_destination(self, tmp_path):
        output = tmp_path / "model-dir"

        with patch("entrypoint.OrasHelper"):
            with pytest.raises(entrypoint.PullError, match="returned no files"):
                entrypoint.pull_from_harbor(
                    harbor_ref="harbor.example.com/project/empty:v1",
                    output_path=output,
                    harbor_url="https://harbor.example.com",
                    username="user",
                    password="pass",
                )

        assert not output.exists()

    def test_pull_timeout_is_reported_and_cleans_staging_directory(self, tmp_path):
        output = tmp_path / "model-dir"
        with patch("entrypoint.OrasHelper") as mock_oras_class:
            mock_oras_class.return_value.pull.side_effect = lambda *_args, **_kwargs: (
                time.sleep(2)
            )

            with pytest.raises(entrypoint.PullError, match="timed out after 1 seconds"):
                entrypoint.pull_from_harbor(
                    harbor_ref="harbor.example.com/project/slow:v1",
                    output_path=output,
                    harbor_url="https://harbor.example.com",
                    username="user",
                    password="pass",
                    timeout_seconds=1,
                )

        assert not output.exists()
        assert not list(tmp_path.glob(".model-dir.download-*"))

    def test_existing_destination_is_not_overwritten(self, tmp_path):
        output = tmp_path / "model-dir"
        output.mkdir()
        (output / "existing.bin").write_bytes(b"existing")

        with pytest.raises(entrypoint.DestinationError, match="already exists"):
            entrypoint.pull_from_harbor(
                harbor_ref="harbor.example.com/project/model:v1",
                output_path=output,
                harbor_url="https://harbor.example.com",
                username="user",
                password="pass",
            )

        assert (output / "existing.bin").read_bytes() == b"existing"


# ---------------------------------------------------------------------------
# Test: job.json update
# ---------------------------------------------------------------------------


class TestUpdateJobConfig:
    def test_atomic_update_adds_step_result(self, tmp_path):
        config_path = tmp_path / "job.json"
        config_path.write_text(
            json.dumps(
                {
                    "job_id": "test-job",
                    "name": "test",
                    "steps": {},
                }
            )
        )

        entrypoint.update_job_config(
            config_path=config_path,
            model_dir="/workspace/models/my-model",
            source_uri="repo://my-model:v1",
            harbor_ref="harbor.example.com/project/my-model:v1",
            digest="sha256:abc123",
            size_bytes=2048,
        )

        updated = json.loads(config_path.read_text())
        step = updated["steps"]["download_model"]
        assert step["status"] == "completed"
        assert step["model_dir"] == "/workspace/models/my-model"
        assert step["source_uri"] == "repo://my-model:v1"
        assert step["harbor_ref"] == "harbor.example.com/project/my-model:v1"
        assert step["digest"] == "sha256:abc123"
        assert step["size_bytes"] == 2048

    def test_missing_file_raises_config_update_error(self, tmp_path):
        missing = tmp_path / "nonexistent.json"

        with pytest.raises(entrypoint.ConfigUpdateError, match="not found"):
            entrypoint.update_job_config(
                config_path=missing,
                model_dir="/workspace/models/x",
                source_uri="repo://x:v1",
                harbor_ref="harbor.example.com/x:v1",
                digest=None,
                size_bytes=0,
            )

    def test_invalid_json_raises(self, tmp_path):
        bad_json = tmp_path / "job.json"
        bad_json.write_text("{not valid json")

        with pytest.raises(entrypoint.ConfigUpdateError, match="not valid JSON"):
            entrypoint.update_job_config(
                config_path=bad_json,
                model_dir="/workspace/models/x",
                source_uri="repo://x:v1",
                harbor_ref="harbor.example.com/x:v1",
                digest=None,
                size_bytes=0,
            )

    def test_no_tmp_file_left_behind_on_success(self, tmp_path):
        config_path = tmp_path / "job.json"
        config_path.write_text(json.dumps({"job_id": "test", "steps": {}}))

        entrypoint.update_job_config(
            config_path=config_path,
            model_dir="/workspace/models/x",
            source_uri="repo://x:v1",
            harbor_ref="harbor.example.com/x:v1",
            digest="sha256:abc",
            size_bytes=0,
        )

        # No .tmp files left in the directory
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Orphaned tmp files: {tmp_files}"


# ---------------------------------------------------------------------------
# Test: helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_compute_dir_size(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world!")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("nested")

        size = entrypoint.compute_dir_size(tmp_path)
        assert size == len("hello") + len("world!") + len("nested")

    def test_compute_dir_size_empty(self, tmp_path):
        size = entrypoint.compute_dir_size(tmp_path)
        assert size == 0

    def test_count_files(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("z")

        assert entrypoint.count_files(tmp_path) == 3

    def test_count_files_empty(self, tmp_path):
        assert entrypoint.count_files(tmp_path) == 0


# ---------------------------------------------------------------------------
# Test: end-to-end integration (mocked externals)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_happy_path(self, tmp_path, monkeypatch):
        """Simulate the full flow with mocked externals."""
        # Set up a fake workspace
        ws = tmp_path / "workspace"
        models = ws / "models"
        config = ws / "config"
        models.mkdir(parents=True)
        config.mkdir(parents=True)

        job_json = config / "job.json"
        job_json.write_text(json.dumps({"job_id": "e2e-test", "steps": {}}))

        output_dir = models / "my-model"

        # Override module-level constants so path validation works with tmp_path
        monkeypatch.setattr(entrypoint, "WORKSPACE_MODELS", models)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", job_json)
        monkeypatch.setenv("MODEL_URI", "repo://my-model:v1")
        monkeypatch.setenv("MODEL_OUTPUT_DIR", str(output_dir))
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        # Mock resolve
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {
                "harbor_ref": "harbor.example.com/project/my-model:v1",
                "checksum": "sha256:abc",
                "size_bytes": 100,
                "name": "my-model",
                "version": "v1",
                "category": "model",
            }
            mock_get.return_value = mock_resp

            # Mock ORAS pull
            with patch("entrypoint.OrasHelper") as mock_oras_class:
                mock_oras = mock_oras_class.return_value

                # Simulate pull: create files in output_dir
                def fake_pull(harbor_ref, outdir):
                    outpath = Path(outdir)
                    outpath.mkdir(parents=True, exist_ok=True)
                    (outpath / "model.safetensors").write_bytes(b"x" * 100)

                mock_oras.pull.side_effect = fake_pull

                entrypoint.main()

        # Verify job.json was updated
        updated = json.loads(job_json.read_text())
        step = updated["steps"]["download_model"]
        assert step["status"] == "completed"
        assert step["source_uri"] == "repo://my-model:v1"
        assert step["harbor_ref"] == "harbor.example.com/project/my-model:v1"
        assert step["size_bytes"] == 100
        assert step["digest"] == "sha256:abc"

    def test_config_update_failure_removes_downloaded_model(
        self, tmp_path, monkeypatch
    ):
        ws = tmp_path / "workspace"
        models = ws / "models"
        config = ws / "config"
        models.mkdir(parents=True)
        config.mkdir(parents=True)
        job_json = config / "job.json"
        job_json.write_text(json.dumps({"job_id": "e2e-test", "steps": {}}))
        output_dir = models / "my-model"

        monkeypatch.setattr(entrypoint, "WORKSPACE_MODELS", models)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", job_json)
        monkeypatch.setenv("MODEL_URI", "repo://my-model:v1")
        monkeypatch.setenv("MODEL_OUTPUT_DIR", str(output_dir))
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with (
            patch.object(
                entrypoint,
                "resolve_model_uri",
                return_value={
                    "harbor_ref": "harbor.example.com/project/my-model:v1",
                    "digest": "sha256:abc",
                    "size_bytes": 100,
                },
            ),
            patch.object(entrypoint, "OrasHelper") as mock_oras_class,
            patch.object(
                entrypoint,
                "update_job_config",
                side_effect=entrypoint.ConfigUpdateError("cannot write config"),
            ),
        ):
            mock_oras_class.return_value.pull.side_effect = lambda _ref, outdir: Path(
                outdir, "model.safetensors"
            ).write_bytes(b"x" * 100)

            with pytest.raises(
                entrypoint.ConfigUpdateError, match="cannot write config"
            ):
                entrypoint.main()

        assert not output_dir.exists()
