"""Tests for upload-model/entrypoint.py.

All external calls (requests.post, OrasHelper.push_custom) are mocked.
Filesystem operations use tmp_path — nothing touches the real disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

# The entrypoint module lives in a directory named with a hyphen
# (upload-model/), which is not a valid Python package name.
# Import it by adding the parent directory to sys.path.
# Pop any cached "entrypoint" from sys.modules first so that
# multiple step test suites can coexist in the same pytest run.
_HERE = Path(__file__).resolve().parent
_STEP_DIR = _HERE.parent  # upload-model/
sys.modules.pop("entrypoint", None)
sys.path.insert(0, str(_STEP_DIR))

import entrypoint  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_required_env(monkeypatch, **overrides):
    """Set all required env vars, allowing per-test overrides."""
    env = {
        "MODEL_SOURCE_PATH": "/workspace/output/base_osl.gguf",
        "HARBOR_TARGET_REF": "imgrepo.damit.hu/supernova/iris-osl:v4",
        "ARTIFACT_NAME": "iris-osl",
        "VERSION": "v4",
        "ARTIFACT_CATEGORY": "model",
        "METADATA_PATH": "/workspace/config/upload-metadata.json",
        "DATA_REPOSITORY_URL": "http://repo:8000",
        "HARBOR_URL": "https://imgrepo.damit.hu",
        "HARBOR_USERNAME": "user",
        "HARBOR_PASSWORD": "pass",
    }
    env.update(overrides)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _mock_push_result(digest="sha256:abc123"):
    result = MagicMock()
    result.digest = digest
    result.status_code = 201
    return result


def _mock_oras_client(digest="sha256:abc123"):
    """Return (mock_oras, mock_client) wired for the flat push sequence."""
    mock_oras = MagicMock()
    mock_client = MagicMock()
    mock_client.upload_blob.return_value = MagicMock()
    mock_client.upload_manifest.return_value = MagicMock(
        headers={"Docker-Content-Digest": digest}
    )
    mock_oras._client = mock_client
    return mock_oras, mock_client


def _mock_registration_response(**overrides):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 201
    resp.json.return_value = {
        "name": "iris-osl",
        "version": "v4",
        "harbor_ref": "imgrepo.damit.hu/supernova/iris-osl:v4",
        "category": "model",
        **overrides,
    }
    return resp


# ---------------------------------------------------------------------------
# Test: missing environment variables
# ---------------------------------------------------------------------------


class TestMissingEnvVars:
    def test_missing_model_source_path_and_no_job_source(self, tmp_path, monkeypatch):
        # MODEL_SOURCE_PATH is optional when job.json provides a source.
        # Here neither is present, so the step must fail clearly.
        config = tmp_path / "workspace" / "config"
        config.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", config / "job.json")
        (config / "job.json").write_text(json.dumps({"job_id": "job-1", "steps": {}}))

        _set_required_env(monkeypatch, MODEL_SOURCE_PATH=None)
        with pytest.raises(entrypoint.MissingEnvError, match="MODEL_SOURCE_PATH"):
            entrypoint.main()

    def test_missing_harbor_target_ref(self, monkeypatch):
        _set_required_env(monkeypatch, HARBOR_TARGET_REF=None)
        with pytest.raises(entrypoint.MissingEnvError, match="HARBOR_TARGET_REF"):
            entrypoint.main()

    def test_missing_artifact_name(self, monkeypatch):
        _set_required_env(monkeypatch, ARTIFACT_NAME=None)
        with pytest.raises(entrypoint.MissingEnvError, match="ARTIFACT_NAME"):
            entrypoint.main()

    def test_invalid_artifact_category(self, monkeypatch):
        _set_required_env(monkeypatch, ARTIFACT_CATEGORY="bogus")
        with pytest.raises(entrypoint.ArtifactCategoryError, match="ARTIFACT_CATEGORY"):
            entrypoint.main()

    def test_missing_data_repo_url(self, monkeypatch):
        _set_required_env(monkeypatch, DATA_REPOSITORY_URL=None)
        with pytest.raises(entrypoint.MissingEnvError, match="DATA_REPOSITORY_URL"):
            entrypoint.main()

    def test_missing_harbor_url(self, monkeypatch):
        _set_required_env(monkeypatch, HARBOR_URL=None)
        with pytest.raises(entrypoint.MissingEnvError, match="HARBOR_URL"):
            entrypoint.main()

    def test_missing_harbor_credentials(self, monkeypatch):
        _set_required_env(monkeypatch, HARBOR_USERNAME=None, HARBOR_PASSWORD=None)
        with pytest.raises(entrypoint.MissingEnvError, match="HARBOR_USERNAME"):
            entrypoint.main()

    def test_cli_returns_nonzero_for_missing_required_env(self):
        env = os.environ.copy()
        for variable in (
            "MODEL_SOURCE_PATH",
            "HARBOR_TARGET_REF",
            "ARTIFACT_NAME",
            "VERSION",
            "ARTIFACT_CATEGORY",
            "METADATA_PATH",
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
        # MODEL_SOURCE_PATH is optional (falls back to job.json), so the first
        # required variable reported is HARBOR_TARGET_REF.
        assert "HARBOR_TARGET_REF is required" in result.stdout


# ---------------------------------------------------------------------------
# Test: path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    def test_valid_path_under_workspace_output(self, tmp_path, monkeypatch):
        output_dir = tmp_path / "workspace" / "output"
        output_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output_dir)
        source = output_dir / "model.gguf"
        source.touch()

        result = entrypoint.validate_source_path(source)
        assert result == source.resolve()

    def test_path_outside_workspace_rejected(self, tmp_path, monkeypatch):
        output_dir = tmp_path / "workspace" / "output"
        output_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output_dir)
        bad_path = tmp_path / "not-workspace" / "model.gguf"
        bad_path.parent.mkdir(parents=True)

        with pytest.raises(entrypoint.PathValidationError, match="must be under"):
            entrypoint.validate_source_path(bad_path)

    def test_path_escape_with_dotdot_rejected(self, tmp_path, monkeypatch):
        output_dir = tmp_path / "workspace" / "output"
        output_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output_dir)
        nested = tmp_path / "workspace" / "output" / ".." / ".." / "escape"
        nested.parent.mkdir(parents=True, exist_ok=True)

        with pytest.raises(entrypoint.PathValidationError, match="must be under"):
            entrypoint.validate_source_path(nested)

    def test_main_rejects_path_outside_workspace(self, tmp_path, monkeypatch):
        _set_required_env(
            monkeypatch,
            MODEL_SOURCE_PATH=str(tmp_path / "not-a-workspace" / "model.gguf"),
        )
        with pytest.raises(entrypoint.PathValidationError, match="must be under"):
            entrypoint.main()


# ---------------------------------------------------------------------------
# Test: source existence
# ---------------------------------------------------------------------------


class TestSourceNotFound:
    def test_missing_source_raises(self, tmp_path, monkeypatch):
        output = tmp_path / "workspace" / "output"
        output.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output)
        _set_required_env(
            monkeypatch,
            MODEL_SOURCE_PATH=str(output / "does-not-exist.gguf"),
        )
        with pytest.raises(entrypoint.SourceNotFoundError, match="does not exist"):
            entrypoint.main()

    def test_empty_source_directory_raises(self, tmp_path, monkeypatch):
        output = tmp_path / "workspace" / "output"
        output.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output)
        empty = output / "checkpoint-empty"
        empty.mkdir()
        _set_required_env(monkeypatch, MODEL_SOURCE_PATH=str(empty))

        with pytest.raises(entrypoint.SourceNotFoundError, match="contains no files"):
            entrypoint.main()

    def test_source_directory_with_only_subdirs_raises(self, tmp_path, monkeypatch):
        output = tmp_path / "workspace" / "output"
        output.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output)
        empty = output / "checkpoint-empty"
        (empty / "nested").mkdir(parents=True)
        _set_required_env(monkeypatch, MODEL_SOURCE_PATH=str(empty))

        with pytest.raises(entrypoint.SourceNotFoundError, match="contains no files"):
            entrypoint.main()

    def test_count_files(self, tmp_path):
        assert entrypoint.count_files(tmp_path) == 0
        (tmp_path / "a.bin").write_bytes(b"x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.bin").write_bytes(b"x")
        assert entrypoint.count_files(tmp_path) == 2
        assert entrypoint.count_files(tmp_path / "a.bin") == 1
        assert entrypoint.count_files(tmp_path / "missing") == 0


# ---------------------------------------------------------------------------
# Test: Harbor target ref validation
# ---------------------------------------------------------------------------


class TestValidateTargetRef:
    def test_matching_host_accepted(self):
        entrypoint.validate_target_ref(
            "imgrepo.damit.hu/supernova/iris-osl:v4", "https://imgrepo.damit.hu"
        )

    def test_host_mismatch_rejected(self):
        with pytest.raises(entrypoint.PushError, match="but HARBOR_URL points at"):
            entrypoint.validate_target_ref(
                "imgrepo.damit.hu/supernova/iris-osl:v4", "https://other.example.com"
            )

    @pytest.mark.parametrize("ref", ["iris-osl:v4", "imgrepo.damit.hu/"])
    def test_incomplete_reference_rejected(self, ref):
        with pytest.raises(entrypoint.PushError, match="full OCI reference"):
            entrypoint.validate_target_ref(ref, "https://imgrepo.damit.hu")

    def test_main_rejects_mismatched_registry(self, monkeypatch):
        _set_required_env(monkeypatch, HARBOR_URL="https://other.example.com")
        with pytest.raises(entrypoint.PushError, match="but HARBOR_URL points at"):
            entrypoint.main()


# ---------------------------------------------------------------------------
# Test: metadata loading and aggregation
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_load_metadata_file_returns_empty_when_none(self):
        assert entrypoint.load_metadata_file(None) == {}

    def test_load_metadata_file_parses_object(self, tmp_path):
        path = tmp_path / "upload-metadata.json"
        path.write_text(json.dumps({"training_config": {"epochs": 3}}))
        assert entrypoint.load_metadata_file(path) == {"training_config": {"epochs": 3}}

    def test_load_metadata_file_missing_is_not_an_error(self, tmp_path):
        # The workspace spec makes upload-metadata.json optional even though
        # the step executor always injects METADATA_PATH.
        assert entrypoint.load_metadata_file(tmp_path / "missing.json") == {}

    def test_load_metadata_file_invalid_json_raises(self, tmp_path):
        path = tmp_path / "upload-metadata.json"
        path.write_text("{not json")
        with pytest.raises(entrypoint.MetadataError, match="not valid JSON"):
            entrypoint.load_metadata_file(path)

    def test_load_metadata_file_non_object_raises(self, tmp_path):
        path = tmp_path / "upload-metadata.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(entrypoint.MetadataError, match="JSON object"):
            entrypoint.load_metadata_file(path)

    def test_aggregate_metadata_adds_source_trainer_from_job_id(self):
        metadata = entrypoint.aggregate_metadata(
            {}, {"job_id": "supernova-job-12345", "steps": {}}
        )
        assert metadata["lineage"]["source_trainer"] == "supernova-job-12345"

    def test_aggregate_metadata_keeps_existing_lineage(self):
        metadata = entrypoint.aggregate_metadata(
            {"lineage": {"source_trainer": "custom-trainer"}},
            {"job_id": "supernova-job-12345", "steps": {}},
        )
        assert metadata["lineage"]["source_trainer"] == "custom-trainer"

    def test_aggregate_metadata_falls_back_to_train_eval_metrics(self):
        metadata = entrypoint.aggregate_metadata(
            {},
            {
                "job_id": "supernova-job-12345",
                "steps": {"train": {"eval_metrics": {"accuracy": 0.95}}},
            },
        )
        assert metadata["eval_metrics"] == {"accuracy": 0.95}

    def test_aggregate_metadata_keeps_metadata_file_eval_metrics(self):
        metadata = entrypoint.aggregate_metadata(
            {"eval_metrics": {"loss": 0.1}},
            {
                "job_id": "supernova-job-12345",
                "steps": {"train": {"eval_metrics": {"accuracy": 0.95}}},
            },
        )
        assert metadata["eval_metrics"] == {"loss": 0.1}


# ---------------------------------------------------------------------------
# Test: source path resolution (env var wins, else job.json fallback)
# ---------------------------------------------------------------------------


class TestResolveSourcePath:
    def test_falls_back_to_train_best_checkpoint_path(self):
        result = entrypoint.resolve_source_path(
            {"steps": {"train": {"best_checkpoint_path": "/workspace/output/ckpt"}}},
        )
        assert result == "/workspace/output/ckpt"

    def test_falls_back_to_convert_model_output_path(self):
        result = entrypoint.resolve_source_path(
            {
                "steps": {
                    "convert_model": {"output_path": "/workspace/output/model.gguf"}
                }
            },
        )
        assert result == "/workspace/output/model.gguf"

    def test_train_takes_precedence_over_convert(self):
        result = entrypoint.resolve_source_path(
            {
                "steps": {
                    "train": {"best_checkpoint_path": "/workspace/output/ckpt"},
                    "convert_model": {"output_path": "/workspace/output/model.gguf"},
                }
            },
        )
        assert result == "/workspace/output/ckpt"

    def test_returns_none_when_no_source(self):
        assert entrypoint.resolve_source_path({"steps": {}}) is None

    def test_returns_none_when_steps_missing(self):
        assert entrypoint.resolve_source_path({}) is None


# ---------------------------------------------------------------------------
# Test: registration endpoint
# ---------------------------------------------------------------------------


class TestRegistrationEndpoint:
    def test_model_category(self):
        url = entrypoint.registration_endpoint("http://repo:8000", "iris-osl", "model")
        assert url == "http://repo:8000/api/models/iris-osl/versions"

    def test_dataset_category(self):
        url = entrypoint.registration_endpoint(
            "http://repo:8000", "iris-tickets", "dataset"
        )
        assert url == "http://repo:8000/api/datasets/iris-tickets/versions"

    def test_invalid_category_raises(self):
        with pytest.raises(entrypoint.ArtifactCategoryError, match="ARTIFACT_CATEGORY"):
            entrypoint.registration_endpoint("http://repo:8000", "x", "bogus")


# ---------------------------------------------------------------------------
# Test: register_version
# ---------------------------------------------------------------------------


class TestRegisterVersion:
    def test_successful_registration(self):
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_registration_response()
            result = entrypoint.register_version(
                "http://repo:8000",
                "iris-osl",
                "model",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v4",
                version="v4",
                digest="sha256:abc123",
                metadata={"training_config": {"epochs": 3}},
            )
        assert result["name"] == "iris-osl"
        assert result["version"] == "v4"

    def test_registration_omits_version_when_none(self):
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_registration_response(version="v5")
            entrypoint.register_version(
                "http://repo:8000",
                "iris-osl",
                "model",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v5",
                version=None,
                digest="sha256:abc123",
                metadata={},
            )
        payload = mock_post.call_args.kwargs["json"]
        assert "version" not in payload

    def test_registration_sends_size_bytes(self):
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_registration_response()
            entrypoint.register_version(
                "http://repo:8000",
                "iris-osl",
                "model",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v4",
                version="v4",
                digest="sha256:abc123",
                metadata={},
                size_bytes=1048576,
            )
        payload = mock_post.call_args.kwargs["json"]
        assert payload["size_bytes"] == 1048576

    def test_registration_omits_size_bytes_when_none(self):
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_registration_response()
            entrypoint.register_version(
                "http://repo:8000",
                "iris-osl",
                "model",
                harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v4",
                version="v4",
                digest="sha256:abc123",
                metadata={},
            )
        payload = mock_post.call_args.kwargs["json"]
        assert "size_bytes" not in payload

    def test_conflict_raises(self):
        resp = MagicMock()
        resp.status_code = 409
        resp.ok = False
        resp.text = "duplicate"
        with patch("requests.post", return_value=resp):
            with pytest.raises(entrypoint.RegistrationError, match="already exists"):
                entrypoint.register_version(
                    "http://repo:8000",
                    "iris-osl",
                    "model",
                    harbor_ref="ref",
                    version="v4",
                    digest="sha256:abc123",
                    metadata={},
                )

    def test_harbor_not_found_raises(self):
        resp = MagicMock()
        resp.status_code = 404
        resp.ok = False
        resp.text = "not found"
        with patch("requests.post", return_value=resp):
            with pytest.raises(entrypoint.RegistrationError, match="could not verify"):
                entrypoint.register_version(
                    "http://repo:8000",
                    "iris-osl",
                    "model",
                    harbor_ref="ref",
                    version="v4",
                    digest="sha256:abc123",
                    metadata={},
                )

    def test_connection_failure_raises(self):
        with patch("requests.post", side_effect=requests.RequestException("boom")):
            with pytest.raises(entrypoint.RegistrationError, match="Failed to reach"):
                entrypoint.register_version(
                    "http://repo:8000",
                    "iris-osl",
                    "model",
                    harbor_ref="ref",
                    version="v4",
                    digest="sha256:abc123",
                    metadata={},
                )


# ---------------------------------------------------------------------------
# Test: push_to_harbor
# ---------------------------------------------------------------------------


class TestPushToHarbor:
    def test_push_emits_one_layer_per_file(self, tmp_path):
        source = tmp_path / "checkpoint"
        source.mkdir()
        (source / "config.json").write_bytes(b"{}")
        (source / "model.gguf").write_bytes(b"\x00" * 64)
        (source / "tokenizer.json").write_bytes(b"{}")
        config = tmp_path / "oci-config.json"
        config.write_text("{}")

        with patch("entrypoint.OrasHelper") as mock_oras_cls:
            mock_oras, mock_client = _mock_oras_client()
            mock_oras_cls.return_value = mock_oras

            digest = entrypoint.push_to_harbor(
                "imgrepo.damit.hu/supernova/iris-osl:v4",
                source,
                config,
                "model",
                "https://harbor.example.com",
                "user",
                "pass",
            )

        assert digest == "sha256:abc123"
        # config blob + one layer per file
        assert mock_client.upload_blob.call_count == 4
        manifest = mock_client.upload_manifest.call_args.args[0]
        assert len(manifest["layers"]) == 3
        for layer in manifest["layers"]:
            assert "org.opencontainers.image.title" in layer["annotations"]

    def test_push_layer_titles_are_relative_posix_paths(self, tmp_path):
        source = tmp_path / "checkpoint"
        (source / "sub" / "dir").mkdir(parents=True)
        (source / "sub" / "dir" / "file.bin").write_bytes(b"x" * 16)
        (source / "config.json").write_bytes(b"{}")
        config = tmp_path / "oci-config.json"
        config.write_text("{}")

        with patch("entrypoint.OrasHelper") as mock_oras_cls:
            mock_oras, mock_client = _mock_oras_client()
            mock_oras_cls.return_value = mock_oras

            entrypoint.push_to_harbor(
                "imgrepo.damit.hu/supernova/iris-osl:v4",
                source,
                config,
                "model",
                "https://harbor.example.com",
                "user",
                "pass",
            )

        manifest = mock_client.upload_manifest.call_args.args[0]
        titles = {
            layer["annotations"]["org.opencontainers.image.title"]
            for layer in manifest["layers"]
        }
        assert titles == {"sub/dir/file.bin", "config.json"}

    def test_push_single_file_source_titles_the_file(self, tmp_path):
        source = tmp_path / "model.gguf"
        source.write_bytes(b"\x00" * 64)
        config = tmp_path / "oci-config.json"
        config.write_text("{}")

        with patch("entrypoint.OrasHelper") as mock_oras_cls:
            mock_oras, mock_client = _mock_oras_client()
            mock_oras_cls.return_value = mock_oras

            entrypoint.push_to_harbor(
                "imgrepo.damit.hu/supernova/iris-osl:v4",
                source,
                config,
                "model",
                "https://harbor.example.com",
                "user",
                "pass",
            )

        # No temp directory was created next to the source.
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "model.gguf",
            "oci-config.json",
        ]
        manifest = mock_client.upload_manifest.call_args.args[0]
        assert len(manifest["layers"]) == 1
        assert (
            manifest["layers"][0]["annotations"]["org.opencontainers.image.title"]
            == "model.gguf"
        )

    def test_push_rejects_path_traversal_title(self, tmp_path, monkeypatch):
        source = tmp_path / "model.gguf"
        source.write_bytes(b"\x00" * 64)
        config = tmp_path / "oci-config.json"
        config.write_text("{}")

        crafted = [(source, "../../etc/passwd")]
        with (
            patch("entrypoint.collect_artifact_files", return_value=crafted),
            pytest.raises(entrypoint.PushError, match=r"\.\."),
        ):
            entrypoint.push_to_harbor(
                "imgrepo.damit.hu/supernova/iris-osl:v4",
                source,
                config,
                "model",
                "https://harbor.example.com",
                "user",
                "pass",
            )

    def test_push_rejects_duplicate_titles(self, tmp_path, monkeypatch):
        source = tmp_path / "model.gguf"
        source.write_bytes(b"\x00" * 64)
        config = tmp_path / "oci-config.json"
        config.write_text("{}")

        dupes = [(source, "model.gguf"), (source, "model.gguf")]
        with (
            patch("entrypoint.collect_artifact_files", return_value=dupes),
            pytest.raises(entrypoint.PushError, match="[Dd]uplicate"),
        ):
            entrypoint.push_to_harbor(
                "imgrepo.damit.hu/supernova/iris-osl:v4",
                source,
                config,
                "model",
                "https://harbor.example.com",
                "user",
                "pass",
            )

    def test_push_skips_symlinks(self, tmp_path, monkeypatch):
        source = tmp_path / "checkpoint"
        source.mkdir()
        (source / "model.gguf").write_bytes(b"\x00" * 64)
        real = tmp_path / "real.bin"
        real.write_bytes(b"\x00" * 32)
        (source / "link.bin").symlink_to(real)
        config = tmp_path / "oci-config.json"
        config.write_text("{}")

        with patch("entrypoint.OrasHelper") as mock_oras_cls:
            mock_oras, mock_client = _mock_oras_client()
            mock_oras_cls.return_value = mock_oras

            entrypoint.push_to_harbor(
                "imgrepo.damit.hu/supernova/iris-osl:v4",
                source,
                config,
                "model",
                "https://harbor.example.com",
                "user",
                "pass",
            )

        manifest = mock_client.upload_manifest.call_args.args[0]
        titles = [
            layer["annotations"]["org.opencontainers.image.title"]
            for layer in manifest["layers"]
        ]
        assert titles == ["model.gguf"]

    def test_config_blob_uses_supernova_media_type(self, tmp_path):
        source = tmp_path / "model.gguf"
        source.write_bytes(b"\x00" * 64)
        config = tmp_path / "oci-config.json"
        config.write_text("{}")

        for category, expected in (
            ("model", "application/vnd.supernova.model.config.v1+json"),
            ("dataset", "application/vnd.supernova.dataset.config.v1+json"),
        ):
            with patch("entrypoint.OrasHelper") as mock_oras_cls:
                mock_oras, mock_client = _mock_oras_client()
                mock_oras_cls.return_value = mock_oras

                entrypoint.push_to_harbor(
                    "imgrepo.damit.hu/supernova/iris-osl:v4",
                    source,
                    config,
                    category,
                    "https://harbor.example.com",
                    "user",
                    "pass",
                )

            manifest = mock_client.upload_manifest.call_args.args[0]
            assert manifest["config"]["mediaType"] == expected
            # The config blob was uploaded first, with the same media type.
            first_call = mock_client.upload_blob.call_args_list[0]
            assert first_call.args[0] == str(config)
            assert first_call.args[2]["mediaType"] == expected

    def test_push_with_empty_digest_raises(self, tmp_path):
        source = tmp_path / "model.gguf"
        source.write_bytes(b"\x00" * 64)
        config = tmp_path / "oci-config.json"
        config.write_text("{}")

        with patch("entrypoint.OrasHelper") as mock_oras_cls:
            mock_oras, _client = _mock_oras_client(digest="")
            mock_oras_cls.return_value = mock_oras

            with pytest.raises(entrypoint.PushError, match="no digest"):
                entrypoint.push_to_harbor(
                    "imgrepo.damit.hu/supernova/iris-osl:v4",
                    source,
                    config,
                    "model",
                    "https://harbor.example.com",
                    "user",
                    "pass",
                )

    def test_push_harbor_error_raises(self, tmp_path):
        source = tmp_path / "model.gguf"
        source.write_bytes(b"\x00" * 64)
        config = tmp_path / "oci-config.json"
        config.write_text("{}")

        with patch("entrypoint.OrasHelper") as mock_oras_cls:
            mock_oras = mock_oras_cls.return_value
            mock_oras._client.upload_blob.side_effect = entrypoint.HarborError(
                "auth failed", status_code=401
            )

            with pytest.raises(entrypoint.PushError, match="ORAS push failed"):
                entrypoint.push_to_harbor(
                    "imgrepo.damit.hu/supernova/iris-osl:v4",
                    source,
                    config,
                    "model",
                    "https://harbor.example.com",
                    "user",
                    "pass",
                )


# ---------------------------------------------------------------------------
# Test: update_job_config
# ---------------------------------------------------------------------------


class TestUpdateJobConfig:
    def test_updates_steps_upload_model(self, tmp_path):
        config_path = tmp_path / "job.json"
        config_path.write_text(json.dumps({"job_id": "job-1", "steps": {}}))

        entrypoint.update_job_config(
            config_path,
            harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v4",
            digest="sha256:abc123",
            size_bytes=1024,
            version="v4",
            registration={"name": "iris-osl", "version": "v4"},
        )

        config = json.loads(config_path.read_text())
        step = config["steps"]["upload_model"]
        assert step["status"] == "completed"
        assert step["harbor_ref"] == "imgrepo.damit.hu/supernova/iris-osl:v4"
        assert step["digest"] == "sha256:abc123"
        assert step["size_bytes"] == 1024
        assert step["version"] == "v4"
        assert step["registration"]["name"] == "iris-osl"

    def test_missing_job_config_raises(self, tmp_path):
        with pytest.raises(entrypoint.ConfigUpdateError, match="job.json not found"):
            entrypoint.update_job_config(
                tmp_path / "missing.json",
                harbor_ref="ref",
                digest="sha256:abc123",
                size_bytes=1024,
                version="v4",
                registration={},
            )

    def test_non_object_steps_raises(self, tmp_path):
        config_path = tmp_path / "job.json"
        config_path.write_text(json.dumps({"steps": "not-an-object"}))

        with pytest.raises(entrypoint.ConfigUpdateError, match="non-object steps"):
            entrypoint.update_job_config(
                config_path,
                harbor_ref="ref",
                digest="sha256:abc123",
                size_bytes=1024,
                version="v4",
                registration={},
            )


# ---------------------------------------------------------------------------
# Test: full main flow
# ---------------------------------------------------------------------------


class TestMainFlow:
    def test_successful_upload_and_registration(self, tmp_path, monkeypatch):
        output = tmp_path / "workspace" / "output"
        config = tmp_path / "workspace" / "config"
        output.mkdir(parents=True)
        config.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", config / "job.json")

        source = output / "base_osl.gguf"
        source.write_bytes(b"\x00" * 128)

        metadata_path = config / "upload-metadata.json"
        metadata_path.write_text(json.dumps({"training_config": {"epochs": 3}}))

        (config / "job.json").write_text(
            json.dumps(
                {
                    "job_id": "supernova-job-12345",
                    "steps": {"train": {"eval_metrics": {"accuracy": 0.95}}},
                }
            )
        )

        _set_required_env(
            monkeypatch,
            MODEL_SOURCE_PATH=str(source),
            METADATA_PATH=str(metadata_path),
        )

        with (
            patch("entrypoint.OrasHelper") as mock_oras_cls,
            patch("requests.post") as mock_post,
        ):
            mock_oras, _mock_client = _mock_oras_client()
            mock_oras_cls.return_value = mock_oras
            mock_post.return_value = _mock_registration_response()

            entrypoint.main()

        # Verify registration payload
        payload = mock_post.call_args.kwargs["json"]
        assert payload["harbor_ref"] == "imgrepo.damit.hu/supernova/iris-osl:v4"
        assert payload["checksum"] == "sha256:abc123"
        assert payload["metadata"]["training_config"] == {"epochs": 3}
        assert payload["metadata"]["lineage"]["source_trainer"] == "supernova-job-12345"
        # The flat layout stores the source bytes verbatim, so the summed
        # source size is the truthful artifact size.
        assert payload["size_bytes"] == 128

        # Verify job.json update
        job = json.loads((config / "job.json").read_text())
        step = job["steps"]["upload_model"]
        assert step["status"] == "completed"
        assert step["harbor_ref"] == "imgrepo.damit.hu/supernova/iris-osl:v4"
        assert step["size_bytes"] == 128

        # The OCI config layer is staged and removed, not left in config/.
        assert sorted(p.name for p in config.iterdir()) == [
            "job.json",
            "upload-metadata.json",
        ]

    def test_missing_metadata_file_is_tolerated(self, tmp_path, monkeypatch):
        # The step executor always injects METADATA_PATH, but the train step
        # only optionally writes upload-metadata.json.
        output = tmp_path / "workspace" / "output"
        config = tmp_path / "workspace" / "config"
        output.mkdir(parents=True)
        config.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", config / "job.json")

        source = output / "base_osl.gguf"
        source.write_bytes(b"\x00" * 128)
        (config / "job.json").write_text(
            json.dumps({"job_id": "supernova-job-12345", "steps": {}})
        )

        _set_required_env(
            monkeypatch,
            MODEL_SOURCE_PATH=str(source),
            METADATA_PATH=str(config / "upload-metadata.json"),
        )

        with (
            patch("entrypoint.OrasHelper") as mock_oras_cls,
            patch("requests.post") as mock_post,
        ):
            mock_oras, _mock_client = _mock_oras_client()
            mock_oras_cls.return_value = mock_oras
            mock_post.return_value = _mock_registration_response()

            entrypoint.main()

        payload = mock_post.call_args.kwargs["json"]
        assert payload["metadata"]["lineage"]["source_trainer"] == "supernova-job-12345"

        job = json.loads((config / "job.json").read_text())
        assert job["steps"]["upload_model"]["status"] == "completed"

    def test_push_failure_removes_staged_oci_config(self, tmp_path, monkeypatch):
        output = tmp_path / "workspace" / "output"
        config = tmp_path / "workspace" / "config"
        output.mkdir(parents=True)
        config.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", config / "job.json")

        source = output / "base_osl.gguf"
        source.write_bytes(b"\x00" * 128)
        (config / "job.json").write_text(json.dumps({"job_id": "job-1", "steps": {}}))

        _set_required_env(
            monkeypatch, MODEL_SOURCE_PATH=str(source), METADATA_PATH=None
        )

        with patch("entrypoint.OrasHelper") as mock_oras_cls:
            mock_oras_cls.return_value._client.upload_blob.side_effect = RuntimeError(
                "boom"
            )
            with pytest.raises(entrypoint.PushError):
                entrypoint.main()

        assert [p.name for p in config.iterdir()] == ["job.json"]

    def test_registration_failure_does_not_update_job_config(
        self, tmp_path, monkeypatch
    ):
        output = tmp_path / "workspace" / "output"
        config = tmp_path / "workspace" / "config"
        output.mkdir(parents=True)
        config.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", config / "job.json")

        source = output / "base_osl.gguf"
        source.write_bytes(b"\x00" * 128)

        (config / "job.json").write_text(json.dumps({"job_id": "job-1", "steps": {}}))

        _set_required_env(
            monkeypatch, MODEL_SOURCE_PATH=str(source), METADATA_PATH=None
        )

        resp = MagicMock()
        resp.status_code = 409
        resp.ok = False
        resp.text = "duplicate"

        with (
            patch("entrypoint.OrasHelper") as mock_oras_cls,
            patch("requests.post", return_value=resp),
        ):
            mock_oras, _mock_client = _mock_oras_client()
            mock_oras_cls.return_value = mock_oras

            with pytest.raises(entrypoint.RegistrationError):
                entrypoint.main()

        job = json.loads((config / "job.json").read_text())
        assert "upload_model" not in job["steps"]

    def test_uses_job_json_source_when_env_var_absent(self, tmp_path, monkeypatch):
        # When MODEL_SOURCE_PATH is absent, the step resolves the source from
        # job.json -> steps.train.best_checkpoint_path.
        output = tmp_path / "workspace" / "output"
        config = tmp_path / "workspace" / "config"
        output.mkdir(parents=True)
        config.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORKSPACE_OUTPUT", output)
        monkeypatch.setattr(entrypoint, "WORKSPACE_CONFIG", config / "job.json")

        source = output / "checkpoint-12000"
        source.mkdir()
        (source / "model.safetensors").write_bytes(b"\x00" * 64)

        (config / "job.json").write_text(
            json.dumps(
                {
                    "job_id": "supernova-job-12345",
                    "steps": {
                        "train": {
                            "best_checkpoint_path": str(source),
                            "eval_metrics": {"accuracy": 0.95},
                        }
                    },
                }
            )
        )

        _set_required_env(monkeypatch, MODEL_SOURCE_PATH=None, METADATA_PATH=None)

        with (
            patch("entrypoint.OrasHelper") as mock_oras_cls,
            patch("requests.post") as mock_post,
        ):
            mock_oras, mock_client = _mock_oras_client()
            mock_oras_cls.return_value = mock_oras
            mock_post.return_value = _mock_registration_response()

            entrypoint.main()

        # The source resolved from job.json was pushed flat — no tar layer.
        manifest = mock_client.upload_manifest.call_args.args[0]
        title = manifest["layers"][0]["annotations"]["org.opencontainers.image.title"]
        assert title == "model.safetensors"

        # eval_metrics came from the train step in job.json.
        payload = mock_post.call_args.kwargs["json"]
        assert payload["metadata"]["eval_metrics"] == {"accuracy": 0.95}
