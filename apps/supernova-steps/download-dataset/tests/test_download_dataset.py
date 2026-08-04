"""Tests for download-dataset/entrypoint.py.

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
# (download-dataset/), which is not a valid Python package name.
# Import it by adding the parent directory to sys.path.
# Pop any cached "entrypoint" from sys.modules first so that
# multiple step test suites can coexist in the same pytest run.
_HERE = Path(__file__).resolve().parent
_STEP_DIR = _HERE.parent  # download-dataset/
sys.modules.pop("entrypoint", None)
sys.path.insert(0, str(_STEP_DIR))

import entrypoint  # noqa: E402

# ---------------------------------------------------------------------------
# Test: missing environment variables
# ---------------------------------------------------------------------------


class TestMissingEnvVars:
    def test_missing_dataset_uri(self, monkeypatch):
        monkeypatch.delenv("DATASET_URI", raising=False)
        monkeypatch.setenv("DATASET_OUTPUT_DIR", "/workspace/data/test")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with pytest.raises(entrypoint.MissingEnvError, match="DATASET_URI"):
            entrypoint.main()

    def test_missing_dataset_output_dir(self, monkeypatch):
        monkeypatch.setenv("DATASET_URI", "repo://test:v1")
        monkeypatch.delenv("DATASET_OUTPUT_DIR", raising=False)
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with pytest.raises(entrypoint.MissingEnvError, match="DATASET_OUTPUT_DIR"):
            entrypoint.main()

    def test_missing_data_repo_url(self, monkeypatch):
        monkeypatch.setenv("DATASET_URI", "repo://test:v1")
        monkeypatch.setenv("DATASET_OUTPUT_DIR", "/workspace/data/test")
        monkeypatch.delenv("DATA_REPOSITORY_URL", raising=False)
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with pytest.raises(entrypoint.MissingEnvError, match="DATA_REPOSITORY_URL"):
            entrypoint.main()

    def test_missing_harbor_url(self, monkeypatch):
        monkeypatch.setenv("DATASET_URI", "repo://test:v1")
        monkeypatch.setenv("DATASET_OUTPUT_DIR", "/workspace/data/test")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.delenv("HARBOR_URL", raising=False)
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with pytest.raises(entrypoint.MissingEnvError, match="HARBOR_URL"):
            entrypoint.main()

    def test_missing_harbor_credentials(self, monkeypatch):
        monkeypatch.setenv("DATASET_URI", "repo://test:v1")
        monkeypatch.setenv("DATASET_OUTPUT_DIR", "/workspace/data/test")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.delenv("HARBOR_USERNAME", raising=False)
        monkeypatch.delenv("HARBOR_PASSWORD", raising=False)

        with pytest.raises(entrypoint.MissingEnvError, match="HARBOR_USERNAME"):
            entrypoint.main()

    def test_missing_harbor_password(self, monkeypatch):
        monkeypatch.setenv("DATASET_URI", "repo://test:v1")
        monkeypatch.setenv("DATASET_OUTPUT_DIR", "/workspace/data/test")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.delenv("HARBOR_PASSWORD", raising=False)

        with pytest.raises(entrypoint.MissingEnvError, match="HARBOR_PASSWORD"):
            entrypoint.main()

    def test_cli_returns_nonzero_for_missing_dataset_uri(self):
        env = os.environ.copy()
        for variable in (
            "DATASET_URI",
            "DATASET_OUTPUT_DIR",
            "DATA_REPOSITORY_URL",
            "HARBOR_URL",
            "HARBOR_USERNAME",
            "HARBOR_PASSWORD",
        ):
            env.pop(variable, None)
        env["PYTHONUNBUFFERED"] = "1"

        result = subprocess.run(
            [sys.executable, str(_STEP_DIR / "entrypoint.py")],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

        assert result.returncode == 1
        assert "DATASET_URI is required" in result.stdout


# ---------------------------------------------------------------------------
# Test: path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    def test_valid_path_under_workspace_data(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "workspace" / "data"
        data_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_DATA", data_dir)
        output_dir = data_dir / "my-dataset"
        output_dir.mkdir()

        result = entrypoint.validate_output_path(output_dir)
        assert result == output_dir.resolve()

    def test_valid_path_under_symlinked_workspace_data(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "backing-data"
        data_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        workspace_data = workspace / "data"
        workspace_data.symlink_to(data_dir, target_is_directory=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_DATA", workspace_data)

        output_dir = workspace_data / "my-dataset"

        assert entrypoint.validate_output_path(output_dir) == output_dir.resolve()

    def test_path_outside_workspace_rejected(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "workspace" / "data"
        data_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_DATA", data_dir)
        bad_path = tmp_path / "not-workspace" / "data" / "my-dataset"
        bad_path.mkdir(parents=True)

        with pytest.raises(entrypoint.PathValidationError, match="must be under"):
            entrypoint.validate_output_path(bad_path)

    def test_path_escape_with_dotdot_rejected(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "workspace" / "data"
        data_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_DATA", data_dir)
        nested = tmp_path / "workspace" / "data" / ".." / ".." / "escape"
        nested.parent.mkdir(parents=True, exist_ok=True)

        with pytest.raises(entrypoint.PathValidationError, match="must be under"):
            entrypoint.validate_output_path(nested)

    def test_main_rejects_path_outside_workspace(self, monkeypatch):
        monkeypatch.setenv("DATASET_URI", "repo://test:v1")
        monkeypatch.setenv("DATASET_OUTPUT_DIR", "/tmp/not-a-workspace-dataset")
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "password")

        with pytest.raises(entrypoint.PathValidationError, match="must be under"):
            entrypoint.main()


# ---------------------------------------------------------------------------
# Test: Data Repository resolution
# ---------------------------------------------------------------------------


class TestResolveDatasetUri:
    def test_successful_resolve(self):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {
                "harbor_ref": "harbor.example.com/project/dataset:v1",
                "name": "dataset",
                "version": "v1",
                "category": "dataset",
                "record_count": 15000,
                "format": "parquet",
            }
            mock_get.return_value = mock_resp

            result = entrypoint.resolve_dataset_uri(
                "repo://dataset:v1", "http://repo:8000"
            )

        assert result["harbor_ref"] == "harbor.example.com/project/dataset:v1"
        assert result["record_count"] == 15000
        assert result["format"] == "parquet"
        mock_get.assert_called_once_with(
            "http://repo:8000/api/resolve",
            params={"uri": "repo://dataset:v1"},
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
                entrypoint.resolve_dataset_uri("repo://bad:v1", "http://repo:8000")

    def test_non_json_response_raises(self):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.side_effect = ValueError("not json")
            mock_resp.text = "plain text"
            mock_get.return_value = mock_resp

            with pytest.raises(entrypoint.ResolveError, match="non-JSON"):
                entrypoint.resolve_dataset_uri("repo://bad:v1", "http://repo:8000")

    def test_missing_harbor_ref_raises(self):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"name": "dataset", "version": "v1"}
            mock_get.return_value = mock_resp

            with pytest.raises(entrypoint.ResolveError, match="harbor_ref"):
                entrypoint.resolve_dataset_uri("repo://dataset:v1", "http://repo:8000")

    def test_connection_error_raises(self):
        import requests as req_mod

        with patch("requests.get", side_effect=req_mod.ConnectionError("timeout")):
            with pytest.raises(entrypoint.ResolveError, match="Failed to reach"):
                entrypoint.resolve_dataset_uri("repo://dataset:v1", "http://repo:8000")

    def test_server_error_raises_resolve_error(self):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock(ok=False, status_code=500, text="server error")
            mock_get.return_value = mock_resp

            with pytest.raises(entrypoint.ResolveError, match="returned 500"):
                entrypoint.resolve_dataset_uri("repo://dataset:v1", "http://repo:8000")

    @pytest.mark.parametrize(
        "response",
        [
            {"harbor_ref": "harbor.example.com/project/dataset:v1", "format": "csv"},
            {
                "harbor_ref": "harbor.example.com/project/dataset:v1",
                "record_count": 1,
            },
            {
                "harbor_ref": "harbor.example.com/project/dataset:v1",
                "record_count": -1,
                "format": "csv",
            },
        ],
    )
    def test_missing_or_invalid_required_metadata_raises(self, response):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock(ok=True)
            mock_resp.json.return_value = response
            mock_get.return_value = mock_resp

            with pytest.raises(entrypoint.ResolveError, match="required|invalid"):
                entrypoint.resolve_dataset_uri("repo://dataset:v1", "http://repo:8000")


# ---------------------------------------------------------------------------
# Test: ORAS pull
# ---------------------------------------------------------------------------


class TestPullFromHarbor:
    @pytest.mark.parametrize("value", ["invalid", "0", "-1"])
    def test_invalid_harbor_timeout_is_rejected(self, monkeypatch, value):
        monkeypatch.setenv("HARBOR_OPERATION_TIMEOUT_SECONDS", value)

        with pytest.raises(entrypoint.PullError, match="positive integer"):
            entrypoint.harbor_operation_timeout_seconds()

    def test_successful_pull(self, tmp_path):
        output = tmp_path / "dataset-dir"
        with patch.object(entrypoint, "OrasHelper") as mock_oras_class:
            mock_oras = mock_oras_class.return_value
            mock_oras.pull.side_effect = lambda _ref, outdir: Path(
                outdir, "train.parquet"
            ).write_bytes(b"dataset")

            entrypoint.pull_from_harbor(
                harbor_ref="harbor.example.com/project/dataset:v1",
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
        assert pull_args == ("harbor.example.com/project/dataset:v1",)
        assert Path(pull_kwargs["outdir"]).parent == output.parent
        assert Path(pull_kwargs["outdir"]).name.startswith(".dataset-dir.download-")
        assert output.exists()

    def test_preserves_non_default_harbor_port(self, tmp_path):
        output = tmp_path / "dataset-dir"
        with patch.object(entrypoint, "OrasHelper") as mock_oras_class:
            mock_oras_class.return_value.pull.side_effect = lambda _ref, outdir: Path(
                outdir, "train.parquet"
            ).write_bytes(b"dataset")

            entrypoint.pull_from_harbor(
                harbor_ref="harbor.example.com:5443/project/dataset:v1",
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
        output = tmp_path / "dataset-dir"
        from harbor_oci_client import HarborError

        with patch.object(entrypoint, "OrasHelper") as mock_oras_class:
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
        output = tmp_path / "dataset-dir"
        from harbor_oci_client import HarborError

        with patch.object(
            entrypoint, "OrasHelper", side_effect=HarborError("auth failed")
        ):
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
        output = tmp_path / "dataset-dir"

        with patch.object(
            entrypoint, "OrasHelper", side_effect=RuntimeError("network failed")
        ):
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
        output = tmp_path / "dataset-dir"

        with patch.object(entrypoint, "OrasHelper"):
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
        output = tmp_path / "dataset-dir"
        with patch.object(entrypoint, "OrasHelper") as mock_oras_class:
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
        assert not list(tmp_path.glob(".dataset-dir.download-*"))

    def test_existing_destination_is_not_overwritten(self, tmp_path):
        output = tmp_path / "dataset-dir"
        output.mkdir()
        (output / "existing.csv").write_bytes(b"existing")

        with pytest.raises(entrypoint.DestinationError, match="already exists"):
            entrypoint.pull_from_harbor(
                harbor_ref="harbor.example.com/project/dataset:v1",
                output_path=output,
                harbor_url="https://harbor.example.com",
                username="user",
                password="pass",
            )

        assert (output / "existing.csv").read_bytes() == b"existing"


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
            dataset_dir="/workspace/data/my-dataset",
            source_uri="repo://my-dataset:v1",
            harbor_ref="harbor.example.com/project/my-dataset:v1",
            record_count=15000,
            dataset_format="parquet",
        )

        updated = json.loads(config_path.read_text())
        step = updated["steps"]["download_dataset"]
        assert step["status"] == "completed"
        assert step["dataset_dir"] == "/workspace/data/my-dataset"
        assert step["source_uri"] == "repo://my-dataset:v1"
        assert step["harbor_ref"] == "harbor.example.com/project/my-dataset:v1"
        assert step["record_count"] == 15000
        assert step["format"] == "parquet"

    def test_missing_file_raises_config_update_error(self, tmp_path):
        missing = tmp_path / "nonexistent.json"

        with pytest.raises(entrypoint.ConfigUpdateError, match="not found"):
            entrypoint.update_job_config(
                config_path=missing,
                dataset_dir="/workspace/data/x",
                source_uri="repo://x:v1",
                harbor_ref="harbor.example.com/x:v1",
                record_count=None,
                dataset_format=None,
            )

    def test_invalid_json_raises(self, tmp_path):
        bad_json = tmp_path / "job.json"
        bad_json.write_text("{not valid json")

        with pytest.raises(entrypoint.ConfigUpdateError, match="not valid JSON"):
            entrypoint.update_job_config(
                config_path=bad_json,
                dataset_dir="/workspace/data/x",
                source_uri="repo://x:v1",
                harbor_ref="harbor.example.com/x:v1",
                record_count=None,
                dataset_format=None,
            )

    def test_no_tmp_file_left_behind_on_success(self, tmp_path):
        config_path = tmp_path / "job.json"
        config_path.write_text(json.dumps({"job_id": "test", "steps": {}}))

        entrypoint.update_job_config(
            config_path=config_path,
            dataset_dir="/workspace/data/x",
            source_uri="repo://x:v1",
            harbor_ref="harbor.example.com/x:v1",
            record_count=0,
            dataset_format="json",
        )

        # No .tmp files left in the directory
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Orphaned tmp files: {tmp_files}"

    def test_non_object_steps_raises_config_update_error(self, tmp_path):
        config_path = tmp_path / "job.json"
        config_path.write_text(json.dumps({"job_id": "test", "steps": []}))

        with pytest.raises(entrypoint.ConfigUpdateError, match="non-object steps"):
            entrypoint.update_job_config(
                config_path=config_path,
                dataset_dir="/workspace/data/x",
                source_uri="repo://x:v1",
                harbor_ref="harbor.example.com/x:v1",
                record_count=1,
                dataset_format="json",
            )


# ---------------------------------------------------------------------------
# Test: helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
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
        data_dir = ws / "data"
        config_dir = ws / "config"
        data_dir.mkdir(parents=True)
        config_dir.mkdir(parents=True)

        job_json = config_dir / "job.json"
        job_json.write_text(json.dumps({"job_id": "e2e-test", "steps": {}}))

        output_dir = data_dir / "my-dataset"

        # Override module-level constants so path validation works with tmp_path
        monkeypatch.setattr(entrypoint, "WORKSPACE_DATA", data_dir)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", job_json)
        monkeypatch.setenv("DATASET_URI", "repo://my-dataset:v1")
        monkeypatch.setenv("DATASET_OUTPUT_DIR", str(output_dir))
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        # Mock resolve
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {
                "harbor_ref": "harbor.example.com/project/my-dataset:v1",
                "name": "my-dataset",
                "version": "v1",
                "category": "dataset",
                "record_count": 15000,
                "format": "parquet",
            }
            mock_get.return_value = mock_resp

            # Mock ORAS pull
            with patch.object(entrypoint, "OrasHelper") as mock_oras_class:
                mock_oras = mock_oras_class.return_value

                # Simulate pull: create files in output_dir
                def fake_pull(harbor_ref, outdir):
                    outpath = Path(outdir)
                    outpath.mkdir(parents=True, exist_ok=True)
                    (outpath / "train.parquet").write_bytes(b"x" * 100)
                    (outpath / "test.parquet").write_bytes(b"x" * 50)

                mock_oras.pull.side_effect = fake_pull

                entrypoint.main()

        # Verify job.json was updated
        updated = json.loads(job_json.read_text())
        step = updated["steps"]["download_dataset"]
        assert step["status"] == "completed"
        assert step["source_uri"] == "repo://my-dataset:v1"
        assert step["harbor_ref"] == "harbor.example.com/project/my-dataset:v1"
        assert step["dataset_dir"] == str(output_dir)
        assert step["record_count"] == 15000
        assert step["format"] == "parquet"

    def test_config_update_failure_removes_downloaded_dataset(
        self, tmp_path, monkeypatch
    ):
        ws = tmp_path / "workspace"
        data_dir = ws / "data"
        config_dir = ws / "config"
        data_dir.mkdir(parents=True)
        config_dir.mkdir(parents=True)
        job_json = config_dir / "job.json"
        job_json.write_text(json.dumps({"job_id": "e2e-test", "steps": {}}))
        output_dir = data_dir / "my-dataset"

        monkeypatch.setattr(entrypoint, "WORKSPACE_DATA", data_dir)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", job_json)
        monkeypatch.setenv("DATASET_URI", "repo://my-dataset:v1")
        monkeypatch.setenv("DATASET_OUTPUT_DIR", str(output_dir))
        monkeypatch.setenv("DATA_REPOSITORY_URL", "http://repo:8000")
        monkeypatch.setenv("HARBOR_URL", "https://harbor.example.com")
        monkeypatch.setenv("HARBOR_USERNAME", "user")
        monkeypatch.setenv("HARBOR_PASSWORD", "pass")

        with (
            patch.object(
                entrypoint,
                "resolve_dataset_uri",
                return_value={
                    "harbor_ref": "harbor.example.com/project/my-dataset:v1",
                    "record_count": 15000,
                    "format": "parquet",
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
                outdir, "train.parquet"
            ).write_bytes(b"x" * 100)

            with pytest.raises(
                entrypoint.ConfigUpdateError, match="cannot write config"
            ):
                entrypoint.main()

        assert not output_dir.exists()
