"""vLLM prompt-cache cleanup: the backend binding of the shared lifecycle.

The lifecycle itself (detach, purge, the live-run guard, the boot sweep) is
covered once in ``test_prompt_cache.py`` through the SGLang descriptor. This
suite pins the vLLM binding: the environment variable ``build_env`` exports,
the process-manager dispatch by ``backend_type``, and the boot sweep reaching
the vLLM root as well.
"""

import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from solar_host.backends.prompt_cache import (
    SGLANG_PROMPT_CACHE,
    VLLM_PROMPT_CACHE,
    discard_all_orphan_prompt_caches,
)
from solar_host.backends.vllm import VllmRunner
from solar_host.config import config_manager
from solar_host.models import InstanceStatus, LogMessage
from solar_host.models.base import Instance
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.models.vllm import VllmConfig
from solar_host.process_manager import ProcessManager

_UUID_A = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _isolated_env(_hermetic_settings, tmp_path, monkeypatch):
    """Point both prompt-cache roots and the global config manager at tmp_path."""
    monkeypatch.setattr("solar_host.config.settings.log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 5.0)
    monkeypatch.setattr(
        "solar_host.config.settings.vllm_prompt_cache_dir", str(tmp_path / "vllm-cache")
    )
    monkeypatch.setattr(
        "solar_host.config.settings.sglang_prompt_cache_dir",
        str(tmp_path / "sglang-cache"),
    )
    config_manager.config_file = tmp_path / "config.json"
    config_manager.instances = {}
    return tmp_path


def _log_msg(seq: int, line: str) -> LogMessage:
    return LogMessage(seq=seq, timestamp="2026-08-06T00:00:00+00:00", line=line)


def _make_instance(
    instance_id: str = "inst-1", status=InstanceStatus.STOPPED
) -> Instance:
    instance = Instance(
        id=instance_id,
        config=VllmConfig(model_path="/models/test", alias="deepseek:flash"),
        status=status,
    )
    config_manager.add_instance(instance)
    return instance


def _build_env(instance_id: str = "inst-1", **overrides: object) -> dict[str, str]:
    overrides.setdefault("alias", "deepseek:flash")
    config = VllmConfig(model_path="/models/test", **overrides)
    instance = SimpleNamespace(config=config, port=8080, id=instance_id)
    return VllmRunner().build_env(instance)


def _wait_for_removal(path: Path, timeout: float = 5.0) -> bool:
    """Deadline-poll *path*'s disappearance (the purge runs on a daemon thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not path.exists():
            return True
        time.sleep(0.05)
    return False


# ── build_env wiring ───────────────────────────────────────────────


def test_build_env_points_the_offload_tier_at_its_own_directory(
    _isolated_env,
) -> None:
    env = _build_env("inst-1")

    cache_dir = Path(_isolated_env / "vllm-cache" / "deepseek-flash-inst-1")
    assert env[VLLM_PROMPT_CACHE.env_var] == str(cache_dir)
    assert cache_dir.is_dir()


def test_no_root_means_no_cache_variable(_isolated_env, monkeypatch) -> None:
    monkeypatch.setattr("solar_host.config.settings.vllm_prompt_cache_dir", "")

    env = _build_env()

    assert VLLM_PROMPT_CACHE.env_var not in env


def test_the_alias_is_made_path_safe(_isolated_env) -> None:
    env = _build_env("inst-9", alias="qwen3.6:8b/merged")

    assert env[VLLM_PROMPT_CACHE.env_var] == str(
        _isolated_env / "vllm-cache" / "qwen3.6-8b-merged-inst-9"
    )


def test_build_env_creates_the_dir_without_deleting_contents(_isolated_env) -> None:
    """Mkdir-only contract: a leftover dir's contents survive build_env.

    Leftover dirs are removed by the teardown paths and the boot detach pass,
    never by build_env, which just (re)creates the dir it points vLLM at.
    """
    cache_dir = Path(_isolated_env / "vllm-cache" / "deepseek-flash-inst-1")
    cache_dir.mkdir(parents=True)
    (cache_dir / "leftover.bin").write_bytes(b"x" * 16)

    env = _build_env("inst-1")

    assert env[VLLM_PROMPT_CACHE.env_var] == str(cache_dir)
    assert (cache_dir / "leftover.bin").read_bytes() == b"x" * 16


def test_extra_env_is_passed_through_and_wins_last(_isolated_env) -> None:
    env = _build_env(
        extra_env={
            VLLM_PROMPT_CACHE.env_var: "/somewhere/else",
            "VLLM_USE_FLASHINFER_MOE_FP4": "1",
        }
    )

    assert env[VLLM_PROMPT_CACHE.env_var] == "/somewhere/else"
    assert env["VLLM_USE_FLASHINFER_MOE_FP4"] == "1"


# ── process-manager dispatch by backend_type ───────────────────────


def _cache_dir(root: Path, instance_id: str = "inst-1") -> Path:
    cache_dir = root / f"deepseek-flash-{instance_id}"
    cache_dir.mkdir(parents=True)
    (cache_dir / "cache.bin").write_bytes(b"x")
    return cache_dir


@pytest.mark.anyio
async def test_stop_running_instance_detaches_the_vllm_dir(_isolated_env) -> None:
    _make_instance("inst-1", status=InstanceStatus.RUNNING)
    cache_dir = _cache_dir(_isolated_env / "vllm-cache")
    manager = ProcessManager()
    manager.log_buffers["inst-1"] = deque([_log_msg(0, "line one")], maxlen=5)
    manager.log_sequences["inst-1"] = 1

    assert await manager.stop_instance("inst-1") is True

    assert not cache_dir.exists()
    assert "inst-1" in manager.log_buffers
    assert manager.log_sequences["inst-1"] == 1


def test_delete_instance_detaches_the_vllm_dir(_isolated_env) -> None:
    _make_instance("inst-1")
    cache_dir = _cache_dir(_isolated_env / "vllm-cache")

    manager = ProcessManager()
    assert manager.delete_instance("inst-1") is True

    assert not cache_dir.exists()
    assert config_manager.get_instance("inst-1") is None


@pytest.mark.anyio
async def test_failed_start_detaches_the_vllm_dir(_isolated_env, monkeypatch) -> None:
    monkeypatch.setattr("solar_host.config.settings.max_retries", 0)
    _make_instance("inst-1")
    cache_dir = _cache_dir(_isolated_env / "vllm-cache")

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("never spawn")

    monkeypatch.setattr("solar_host.backends.vllm.VllmRunner.build_command", _boom)

    manager = ProcessManager()
    assert await manager.start_instance("inst-1") is False

    assert not cache_dir.exists()
    instance = config_manager.get_instance("inst-1")
    assert instance is not None
    assert instance.status == InstanceStatus.FAILED


def test_discard_is_a_noop_while_the_process_is_tracked(_isolated_env) -> None:
    """A live child owns the dir: the idle guard must not detach it."""
    _make_instance("inst-1", status=InstanceStatus.RUNNING)
    cache_dir = _cache_dir(_isolated_env / "vllm-cache")
    manager = ProcessManager()
    manager.processes["inst-1"] = object()  # type: ignore[assignment]

    manager._discard_prompt_cache_if_idle(config_manager.get_instance("inst-1"))

    assert cache_dir.is_dir()
    assert (cache_dir / "cache.bin").read_bytes() == b"x"


def test_a_llamacpp_instance_touches_no_prompt_cache_dir(_isolated_env) -> None:
    """A backend without a persistent prompt cache is a no-op by absence."""
    instance = Instance(
        id="inst-1",
        config=LlamaCppConfig(model="/tmp/test.gguf", alias="test"),
        status=InstanceStatus.STOPPED,
    )
    config_manager.add_instance(instance)
    sglang_shaped = Path(_isolated_env / "sglang-cache" / "deepseek-flash-inst-1")
    vllm_shaped = Path(_isolated_env / "vllm-cache" / "deepseek-flash-inst-1")
    sglang_shaped.mkdir(parents=True)
    vllm_shaped.mkdir(parents=True)
    manager = ProcessManager()

    manager._discard_prompt_cache_now(instance)

    assert sglang_shaped.is_dir()
    assert vllm_shaped.is_dir()


# ── boot sweep reaches both roots ─────────────────────────────────


def test_the_boot_sweep_detaches_every_backend_root(_isolated_env) -> None:
    """Both roots are swept, and a shared root is swept once, not twice."""
    vllm_orphan = Path(_isolated_env / "vllm-cache" / f"deepseek-flash-{_UUID_A}")
    sglang_orphan = Path(_isolated_env / "sglang-cache" / f"qwen3.6-{_UUID_A}")
    vllm_orphan.mkdir(parents=True)
    sglang_orphan.mkdir(parents=True)

    discard_all_orphan_prompt_caches()

    # The detach is synchronous; the purge that follows is asynchronous, so
    # only the trash-prefixed shape of anything still present is asserted.
    assert not vllm_orphan.exists()
    assert not sglang_orphan.exists()
    for root in ("vllm-cache", "sglang-cache"):
        assert all(
            child.name.startswith(".trash-")
            for child in (_isolated_env / root).iterdir()
        )


def test_a_shared_root_is_swept_once(_isolated_env, monkeypatch) -> None:
    """Two descriptors on one root must not double-detach the same child."""
    shared = Path(_isolated_env / "shared-cache")
    orphan = shared / f"deepseek-flash-{_UUID_A}"
    orphan.mkdir(parents=True)
    monkeypatch.setattr("solar_host.config.settings.vllm_prompt_cache_dir", str(shared))
    monkeypatch.setattr(
        "solar_host.config.settings.sglang_prompt_cache_dir", str(shared)
    )

    discard_all_orphan_prompt_caches()

    # The first pass renames it aside; the second finds only the `.trash-`
    # leftover and collects it without a second rename. The purge races us, so
    # only the trash-prefixed shape of anything left is asserted.
    assert not orphan.exists()
    assert all(child.name.startswith(".trash-") for child in shared.iterdir())


# ── descriptor wiring ────────────────────────────────────────────


def test_the_descriptor_names_the_environment_variable() -> None:
    assert VLLM_PROMPT_CACHE.env_var == "VLLM_PROMPT_CACHE_DIR"
    assert VLLM_PROMPT_CACHE.setting == "vllm_prompt_cache_dir"
    assert SGLANG_PROMPT_CACHE.env_var == "SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR"


def test_a_llamacpp_config_resolves_to_no_descriptor() -> None:
    from solar_host.backends.prompt_cache import backend_for

    config = LlamaCppConfig(model="/tmp/test.gguf", alias="test")

    assert backend_for(config) is None
