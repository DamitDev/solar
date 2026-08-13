"""C2 structured start-failure payload: POST /instances/{id}/start answers
500 with {detail, instance_id, exit_code, log_tail} so control can link the
failure to its process logs.

Asserted at the wire level. The fields have to be top-level in the serialized
body: solar-control reads ``body["instance_id"]`` directly, and raising the
payload as an ``HTTPException`` detail would nest it under FastAPI's own
``detail`` key where control cannot see it.
"""

import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.config import config_manager
from solar_host.main import app
from solar_host.models import InstanceStatus
from solar_host.models.llamacpp import LlamaCppConfig

API_KEY = "test-api-key"


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
def _isolated_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("solar_host.config.settings.log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr("solar_host.config.settings.solar_control_url", "")
    monkeypatch.setattr("solar_host.config.settings.api_key", API_KEY)
    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 5.0)
    monkeypatch.setattr("solar_host.config.settings.start_failure_log_tail_lines", 20)
    config_manager.config_file = tmp_path / "config.json"
    config_manager.instances = {}
    return tmp_path


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _headers() -> dict:
    return {"X-API-Key": API_KEY}


def _use_script(monkeypatch, script: str) -> None:
    monkeypatch.setattr(
        "solar_host.process_manager.get_runner_for_config",
        lambda cfg: _ScriptRunner(script),
    )


def test_start_failure_body_is_flat_and_structured(client, monkeypatch):
    """The diagnostic fields are top-level, not nested under "detail"."""
    _use_script(
        monkeypatch,
        "print('loading model', flush=True); "
        "print('fatal: bad config', flush=True); "
        "import sys; sys.exit(7)",
    )
    _make_instance("inst-1")

    resp = client.post("/instances/inst-1/start", headers=_headers())

    assert resp.status_code == 500
    body = resp.json()
    # Guards the regression: an HTTPException detail would make this a dict.
    assert isinstance(body["detail"], str)
    assert body["instance_id"] == "inst-1"
    assert body["exit_code"] == 7
    assert "loading model" in body["log_tail"]
    assert "fatal: bad config" in body["log_tail"]
    assert "Process exited unexpectedly" in body["detail"]


def test_readiness_timeout_reports_the_signal_that_killed_the_process(
    client, monkeypatch
):
    """A backend that runs but never reports readiness is killed by the host.

    ``_handle_child_exit`` cannot claim this exit — the start path pops the
    process before signalling it — so the exit code has to be recorded on the
    timeout path or the payload reports ``null`` and the operator cannot tell
    a hung start from one that died on its own. The value is negative:
    terminated by our signal, not a crash of the backend's own making.
    """
    import signal

    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 0.5)
    monkeypatch.setattr("solar_host.config.settings.max_retries", 1)
    _use_script(
        monkeypatch,
        "print('loading model', flush=True); " "import time; time.sleep(30)",
    )
    _make_instance("inst-1")

    resp = client.post("/instances/inst-1/start", headers=_headers())

    assert resp.status_code == 500
    body = resp.json()
    assert body["instance_id"] == "inst-1"
    assert body["exit_code"] == -signal.SIGTERM
    assert "did not report readiness" in body["detail"]
    assert "loading model" in body["log_tail"]


def test_log_tail_bounded_by_setting(client, monkeypatch):
    monkeypatch.setattr("solar_host.config.settings.start_failure_log_tail_lines", 1)
    _use_script(
        monkeypatch,
        "print('first', flush=True); print('second', flush=True); "
        "import sys; sys.exit(1)",
    )
    _make_instance("inst-1")

    resp = client.post("/instances/inst-1/start", headers=_headers())

    assert resp.status_code == 500
    assert resp.json()["log_tail"] == ["second"]
