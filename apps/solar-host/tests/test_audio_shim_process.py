"""The audio shim's process contract: readiness, /health and child death.

The shim must answer /health while the runtime loads (503), report 200 only when
the runtime can serve, and exit when the runtime dies so the host fails the
instance instead of leaving a dead one routable.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
WHISPER = FIXTURES / "fake_whisper_server.py"
NEMO = FIXTURES / "fake_nemo_server.py"


_RUNTIMES = Path(tempfile.mkdtemp(prefix="solar131-runtimes-"))


def _executable(source: Path) -> Path:
    """Copy a fake into an executable location.

    The shim spawns the runtime binary directly, so the fixture must be an
    executable file with a shebang, exactly like the real binary.
    """
    target = _RUNTIMES / source.name
    if not target.exists():
        target.write_text(source.read_text())
        target.chmod(0o755)
    return target


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _shim(model_type: str, binary: Path, port: int, **env: str):
    command = [
        sys.executable,
        "-m",
        "solar_host.servers.audio_server",
        "--model-type",
        model_type,
        "--runtime-binary",
        str(binary),
        "--model",
        "/tmp/model.bin",
        "--alias",
        "whisper-tiny",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--device",
        "cpu",
        "--runtime-timeout-s",
        "5",
        "--request-timeout-s",
        "30",
    ]
    if model_type == "nemospeech":
        command.extend(["--language", "en"])
    # A file, not a pipe: a chatty runtime would fill a 64 KiB pipe and block.
    log = _RUNTIMES / f"shim-{port}.log"
    handle = log.open("w")
    process = subprocess.Popen(
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, **env},
        start_new_session=True,
    )
    process.shim_log = log  # type: ignore[attr-defined]
    return process


def _wait_health_for_code(port: int, code: str, timeout: float = 15.0) -> dict:
    """Wait for a 503 whose error code is ``code`` (not just any 503)."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            last = {"status": response.status_code, "body": response.json()}
            if response.status_code == 503 and (
                last["body"].get("error", {}).get("code") == code
            ):
                return last
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"/health never reported {code}: {last}")


def _wait_health(port: int, expected: int, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            last = {"status": response.status_code, "body": response.json()}
            if response.status_code == expected:
                return last
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"/health never answered {expected}: {last}")


def _log_tail(process: subprocess.Popen) -> str:
    log = getattr(process, "shim_log", None)
    if log is None or not log.exists():
        return ""
    return log.read_text()[-2000:]


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_health_is_503_while_the_runtime_loads():
    port = _free_port()
    process = _shim(
        "whispercpp", _executable(WHISPER), port, FAKE_RUNTIME_BANNER_DELAY_S="2"
    )
    try:
        starting = _wait_health(port, 503)
        assert starting["body"]["error"]["code"] == "runtime_not_ready"
        assert starting["body"]["error"]["message"] == "runtime is starting"

        healthy = _wait_health(port, 200)
        assert healthy["body"]["status"] == "healthy"
        assert healthy["body"]["model"] == "whisper-tiny"
    finally:
        _stop(process)


@pytest.mark.parametrize(
    ("model_type", "binary"),
    [("whispercpp", WHISPER), ("nemospeech", NEMO)],
)
def test_health_is_200_once_the_runtime_is_ready(model_type, binary):
    port = _free_port()
    process = _shim(model_type, _executable(binary), port)
    try:
        healthy = _wait_health(port, 200)
        assert healthy["body"]["status"] == "healthy"
        assert healthy["body"]["model_type"] == model_type
    finally:
        _stop(process)


@pytest.mark.parametrize(
    ("model_type", "binary"),
    [("whispercpp", WHISPER), ("nemospeech", NEMO)],
)
def test_health_reports_a_runtime_that_never_becomes_ready(model_type, binary):
    port = _free_port()
    process = _shim(model_type, _executable(binary), port, FAKE_RUNTIME_NEVER_READY="1")
    try:
        failed = _wait_health_for_code(port, "runtime_failed", timeout=15)
        assert "did not become ready" in failed["body"]["error"]["message"]
    finally:
        _stop(process)


def test_the_shim_exits_when_the_runtime_dies():
    port = _free_port()
    process = _shim("whispercpp", _executable(WHISPER), port)
    try:
        _wait_health(port, 200)
        # Kill the fake runtime, not the shim: the shim's watchdog must exit
        # so the host's process manager observes the exit and fails the
        # instance instead of leaving a dead runtime routable.
        # The child listens on its own loopback port, not the shim's, so match
        # the fixture name rather than a port.
        children = subprocess.run(
            ["pgrep", "-f", f"{WHISPER.name} -m /tmp/model.bin"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert children.stdout.strip(), "the fake runtime was not running"
        for pid in children.stdout.split():
            os.kill(int(pid), 15)
        process.wait(timeout=10)
        assert process.returncode is not None
    finally:
        _stop(process)


def test_a_runtime_that_exits_at_start_fails_the_shim_start():
    port = _free_port()
    process = _shim(
        "whispercpp",
        _executable(WHISPER),
        port,
        FAKE_RUNTIME_EXIT_IMMEDIATELY="1",
    )
    try:
        failed = _wait_health_for_code(port, "runtime_failed", timeout=15)
        assert "before becoming ready" in failed["body"]["error"]["message"]
    finally:
        _stop(process)
