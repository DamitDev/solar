"""Tests for GGUF file pattern resolution (resolve_model_file + config parsing).

Covers the ``model_file`` selector that lets a llama.cpp intent point at one
GGUF inside a pulled model directory, and the same resolution applied to
``mmproj``.
"""

from pathlib import Path

import pytest

from solar_host.config import parse_instance_config
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.models_manager import (
    ManifestEntry,
    add_manifest_entry,
    ensure_models_dir,
    resolve_model_file,
)


def _write(path: Path, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def models_dir(tmp_path: Path, monkeypatch) -> Path:
    """Point settings.models_dir at a temporary directory."""
    target = (tmp_path / "models").resolve()
    target.mkdir()
    monkeypatch.setattr("solar_host.config.settings.models_dir", str(target))
    return target


class TestResolveModelFile:
    def test_absolute_path_is_used_as_is(self, tmp_path: Path):
        elsewhere = _write(tmp_path / "elsewhere" / "model.gguf")
        assert resolve_model_file(tmp_path / "repo", str(elsewhere)) == elsewhere

    def test_missing_absolute_path_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            resolve_model_file(tmp_path, str(tmp_path / "nope.gguf"))

    def test_exact_relative_path(self, tmp_path: Path):
        target = _write(tmp_path / "quant" / "model.gguf")
        assert resolve_model_file(tmp_path, "quant/model.gguf") == target

    def test_root_glob(self, tmp_path: Path):
        target = _write(tmp_path / "Model-UD-Q4_K_XL.gguf")
        _write(tmp_path / "Model-Q8_0.gguf")
        assert resolve_model_file(tmp_path, "*UD-Q4_K_XL*.gguf") == target

    def test_bare_filename_found_in_subdirectory(self, tmp_path: Path):
        target = _write(tmp_path / "UD-Q4_K_XL" / "mmproj-BF16.gguf")
        assert resolve_model_file(tmp_path, "mmproj-BF16.gguf") == target

    def test_glob_matches_nested_file(self, tmp_path: Path):
        target = _write(tmp_path / "UD-Q4_K_XL" / "Model-UD-Q4_K_XL.gguf")
        assert resolve_model_file(tmp_path, "*UD-Q4_K_XL*.gguf") == target

    def test_root_match_wins_over_nested(self, tmp_path: Path):
        root = _write(tmp_path / "model.gguf")
        _write(tmp_path / "nested" / "model.gguf", size=999)
        assert resolve_model_file(tmp_path, "model.gguf") == root

    def test_split_gguf_resolves_to_first_shard(self, tmp_path: Path):
        first = _write(tmp_path / "Model-UD-Q4_K_XL-00001-of-00003.gguf")
        _write(tmp_path / "Model-UD-Q4_K_XL-00002-of-00003.gguf")
        _write(tmp_path / "Model-UD-Q4_K_XL-00003-of-00003.gguf")
        assert resolve_model_file(tmp_path, "*UD-Q4_K_XL*") == first

    def test_largest_match_wins(self, tmp_path: Path):
        big = _write(tmp_path / "Model-Q4-big.gguf", size=4096)
        _write(tmp_path / "Model-Q4-small.gguf", size=8)
        assert resolve_model_file(tmp_path, "Model-Q4-*.gguf") == big

    def test_ambiguous_same_size_matches_raise(self, tmp_path: Path):
        _write(tmp_path / "Model-Q4-a.gguf", size=64)
        _write(tmp_path / "Model-Q4-b.gguf", size=64)
        with pytest.raises(ValueError, match="ambiguous"):
            resolve_model_file(tmp_path, "Model-Q4-*.gguf")

    def test_no_match_raises(self, tmp_path: Path):
        _write(tmp_path / "model.gguf")
        with pytest.raises(FileNotFoundError, match="No file matching"):
            resolve_model_file(tmp_path, "*Q8_0*.gguf")

    def test_directories_are_never_returned(self, tmp_path: Path):
        (tmp_path / "model.gguf").mkdir()
        with pytest.raises(FileNotFoundError):
            resolve_model_file(tmp_path, "model.gguf")


class TestParseInstanceConfigModelFile:
    def _pulled_repo(self, models_dir: Path, source_uri: str) -> Path:
        """Register a pulled HuggingFace snapshot in the manifest."""
        ensure_models_dir()
        repo_dir = models_dir / "hf--unsloth--Model-GGUF"
        repo_dir.mkdir(parents=True, exist_ok=True)
        add_manifest_entry(
            ManifestEntry(
                slug=repo_dir.name,
                source_uri=source_uri,
                path=str(repo_dir.resolve()),
                size_bytes=0,
                downloaded_at="2026-01-01T00:00:00+00:00",
            )
        )
        return repo_dir

    def test_model_file_replaces_the_directory_path(self, models_dir: Path):
        source_uri = "huggingface://unsloth/Model-GGUF"
        repo_dir = self._pulled_repo(models_dir, source_uri)
        target = _write(repo_dir / "UD-Q4_K_XL" / "Model-UD-Q4_K_XL.gguf")

        config = parse_instance_config(
            {
                "backend_type": "llamacpp",
                "alias": "model:q4",
                "model_source": source_uri,
                "model": str(repo_dir),
                "model_file": "*UD-Q4_K_XL*.gguf",
            }
        )

        assert isinstance(config, LlamaCppConfig)
        assert config.model == str(target)

    def test_mmproj_filename_resolves_in_the_repo(self, models_dir: Path):
        source_uri = "huggingface://unsloth/Model-GGUF"
        repo_dir = self._pulled_repo(models_dir, source_uri)
        _write(repo_dir / "Model-UD-Q4_K_XL.gguf")
        mmproj = _write(repo_dir / "mmproj-BF16.gguf")

        config = parse_instance_config(
            {
                "backend_type": "llamacpp",
                "alias": "model:q4",
                "model_source": source_uri,
                "model": str(repo_dir),
                "model_file": "*UD-Q4_K_XL*.gguf",
                "mmproj": "mmproj-BF16.gguf",
            }
        )

        assert config.mmproj == str(mmproj)

    def test_spec_draft_model_filename_resolves_in_the_repo(self, models_dir: Path):
        source_uri = "huggingface://unsloth/Model-GGUF"
        repo_dir = self._pulled_repo(models_dir, source_uri)
        _write(repo_dir / "Model-UD-Q4_K_XL.gguf")
        draft = _write(repo_dir / "Model-DSpark.gguf")

        config = parse_instance_config(
            {
                "backend_type": "llamacpp",
                "alias": "model:q4",
                "model_source": source_uri,
                "model": str(repo_dir),
                "model_file": "*UD-Q4_K_XL*.gguf",
                "spec_type": "draft-dspark",
                "spec_draft_model": "Model-DSpark.gguf",
                "spec_draft_n_max": 7,
            }
        )

        assert config.spec_draft_model == str(draft)

    def test_resolves_against_local_source_directory(self, models_dir: Path):
        repo_dir = models_dir / "local-repo"
        target = _write(repo_dir / "Model-Q8_0.gguf")

        config = parse_instance_config(
            {
                "backend_type": "llamacpp",
                "alias": "model:q8",
                "model_source": "local://local-repo",
                "model_file": "*Q8_0*",
            }
        )

        assert config.model == str(target)

    def test_unresolvable_pattern_raises_when_strict(self, models_dir: Path):
        source_uri = "huggingface://unsloth/Model-GGUF"
        repo_dir = self._pulled_repo(models_dir, source_uri)
        _write(repo_dir / "Model-Q8_0.gguf")

        with pytest.raises(ValueError, match="Cannot resolve model pattern"):
            parse_instance_config(
                {
                    "backend_type": "llamacpp",
                    "alias": "model:q4",
                    "model_source": source_uri,
                    "model": str(repo_dir),
                    "model_file": "*UD-Q4_K_XL*.gguf",
                }
            )

    def test_unresolvable_pattern_keeps_config_when_not_strict(self, models_dir: Path):
        """A config reload must not drop an instance whose file went missing."""
        source_uri = "huggingface://unsloth/Model-GGUF"
        repo_dir = self._pulled_repo(models_dir, source_uri)
        recorded = str(repo_dir / "gone" / "Model-UD-Q4_K_XL.gguf")

        config = parse_instance_config(
            {
                "backend_type": "llamacpp",
                "alias": "model:q4",
                "model_source": source_uri,
                "model": recorded,
                "model_file": "*UD-Q4_K_XL*.gguf",
            },
            strict=False,
        )

        assert config.model == recorded

    def test_absolute_model_without_pattern_is_untouched(self, models_dir: Path):
        target = _write(models_dir / "repo" / "model.gguf")
        config = parse_instance_config(
            {
                "backend_type": "llamacpp",
                "alias": "model:q4",
                "model": str(target),
            }
        )
        assert config.model == str(target)
