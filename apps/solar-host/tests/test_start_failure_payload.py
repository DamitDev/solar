"""C2 structured start-failure payload: POST /instances/{id}/start answers
500 with {detail, instance_id, exit_code, log_tail} so control can link the
failure to its process logs."""

import sys
from typing import cast

import pytest

from fastapi import HTTPException

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.config import config_manager, settings
from solar_host.models import InstanceStatus
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.process_manager import ProcessManager


class _ScriptRunner(LlamaCppRunner):
    def __init__(self, script: str, ready_marker: str = "READY_MARKER"):
        super().__init__()
        self._script = script
        self._ready_marker = ready_marker

    def build_command(self, instance) -> list[str]:
        return [sys.executable, "-u", "-c", self._script]

    def is_ready_line(self, line: str) -> bool:
        return self._ready_marker in line


def _make_instance(instance_id: str = "inst-1", status=InstanceStatus.STOPPED):
    from solar_host.models.base import Instance

    instance = Instance(
        id=instance_id,
        config=LlamaCppConfig(model="/tmp/test.gguf", alias="test"),
        status=status,
    )
    config_manager.add_instance(instance)
    return instance


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr("solar_host.config.settings.log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 5.0)
    monkeypatch.setattr("solar_host.config.settings.start_failure_log_tail_lines", 20)
    config_manager.config_file = tmp_path / "config.json"
    config_manager.instances = {}
    return tmp_path


@pytest.mark.anyio
async def test_start_failure_body_is_structured(_isolated_env, monkeypatch):
    from solar_host.routes.instances import start_instance

    script = (
        "print('loading model', flush=True); "
        "print('fatal: bad config', flush=True); "
        "import sys; sys.exit(7)"
    )
    monkeypatch.setattr(
        "solar_host.process_manager.get_runner_for_config",
        lambda cfg: _ScriptRunner(script),
    )
    manager = ProcessManager()
    manager.instance_runners["inst-1"] = _ScriptRunner(script)
    _make_instance("inst-1")

    with pytest.raises(HTTPException) as excinfo:
        await start_instance("inst-1")

    assert excinfo.value.status_code == 500
    body: dict = cast(dict, excinfo.value.detail)  # structured body at runtime
    assert body["instance_id"] == "inst-1"
    assert body["exit_code"] == 7
    assert "loading model" in body["log_tail"]
    assert "fatal: bad config" in body["log_tail"]
    assert "Process exited unexpectedly" in body["detail"]


@pytest.mark.anyio
async def test_log_tail_bounded_by_setting(_isolated_env, monkeypatch):
    from solar_host.routes.instances import start_instance

    monkeypatch.setattr("solar_host.config.settings.start_failure_log_tail_lines", 1)
    script = (
        "print('first', flush=True); print('second', flush=True); "
        "import sys; sys.exit(1)"
    )
    monkeypatch.setattr(
        "solar_host.process_manager.get_runner_for_config",
        lambda cfg: _ScriptRunner(script),
    )
    manager = ProcessManager()
    manager.instance_runners["inst-1"] = _ScriptRunner(script)
    _make_instance("inst-1")

    with pytest.raises(HTTPException) as excinfo:
        await start_instance("inst-1")

    body: dict = cast(dict, excinfo.value.detail)
    assert body["log_tail"] == ["second"]
