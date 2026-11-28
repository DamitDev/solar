#!/usr/bin/env python3
"""Audio shim server for the GGML speech runtimes.

Each whisper.cpp and NeMo-Speech.cpp instance is one of these shims. The shim
owns the OpenAI transcription contract and forwards the upload to the child
runtime, because neither child speaks the whole contract:

- ``whisper-server`` serves ``/inference`` rather than
  ``/v1/audio/transcriptions``, silently falls back to plain JSON for an unknown
  ``response_format``, and cannot report diarization at all.
- ``nemo-speech serve`` serves ``/v1/audio/transcriptions`` but only a subset of
  the formats, with no ``diarized_json`` and no segment-level timestamps.

The shim also hides the runtime's startup from solar-control: it reports 503 until
the child answers its own health/readiness route, prints ``[READY] runtime ready``
only then (the host's readiness signal), and terminates itself when the child dies so
the host observes the exit instead of leaving a dead instance routable.

Usage:
    python -m solar_host.servers.audio_server \\
        --model-type whispercpp \\
        --runtime-binary /path/to/whisper-server \\
        --model /models/ggml-tiny.bin \\
        --alias whisper-tiny \\
        --port 3501
"""

import argparse
import asyncio
import collections
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiohttp
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from solar_host.memory_monitor import detect_gpu_type
from solar_host.servers.api_key import resolve_api_key
from solar_host.servers.audio_fields import (
    ACCEPTED_FIELDS,
    NEMO_ONLY_FIELDS,
    OPENAI_ARRAY_FIELDS,
    OPENAI_OPTIONAL_FIELDS,
    WHISPER_ONLY_FIELDS,
    collect_list,
    keywords_prompt,
    single_language,
    speech_contexts_json,
)
from solar_host.servers.audio_formats import (
    FORMAT_MEDIA_TYPES,
    RESPONSE_FORMATS,
    TIMESTAMP_GRANULARITIES,
    normalize_language,
)

logger = logging.getLogger(__name__)


_WAVE_FORMAT_NAMES: dict[int, str] = {
    1: "PCM",
    3: "IEEE float",
    6: "A-law",
    7: "mu-law",
    0xFFFE: "WAVE_FORMAT_EXTENSIBLE",
}

_NEMO_WAVE_REQUIREMENTS = "16 kHz mono 16-bit PCM"
_WAVE_DATA_BEFORE_FMT_PROBLEM = (
    "the fmt chunk must precede the data chunk in a WAV upload"
)


@dataclass(frozen=True)
class WaveFormat:
    """The fields of a RIFF/WAVE ``fmt `` chunk the shim validates."""

    audio_format: int
    channels: int
    sample_rate: int
    bits_per_sample: int


class ShimState:
    """Mutable runtime state of one shim process."""

    def __init__(self) -> None:
        self.alias: str = ""
        self.model_type: str = "whispercpp"
        self.model: str = ""
        self.language: str | None = None
        self.device: str = "auto"
        self.encoder: str | None = None
        self.instruction: str | None = None
        self.translate_to: str | None = None
        self.diar_model: str | None = None
        self.diarize: bool = False
        self.api_key: str = ""
        self.child: subprocess.Popen | None = None
        self.child_port: int = 0
        self.child_ready: bool = False
        self.child_error: str | None = None
        self.child_tail: collections.deque[str] = collections.deque(maxlen=20)
        self.watch_task: asyncio.Task | None = None
        self.session: aiohttp.ClientSession | None = None
        self.ready_task: asyncio.Task | None = None
        self.request_timeout_s: float = 1800.0
        self.runtime_timeout_s: float = 600.0


state = ShimState()

security = HTTPBearer(auto_error=False)


def _pdeathsig() -> None:  # pragma: no cover - Linux-only child setup
    """Ask the kernel to SIGTERM the child when the shim dies."""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001, S110
        pass


def _free_loopback_port() -> int:
    """Reserve a free loopback port and release it for the child to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _child_command(args: argparse.Namespace) -> list[str]:
    """Build the child runtime command for the selected model type."""
    if args.model_type == "whispercpp":
        cmd = [
            args.runtime_binary,
            "-m",
            state.model,
            "--host",
            "127.0.0.1",
            "--port",
            str(state.child_port),
            "-t",
            str(args.threads),
            "-l",
            state.language or "auto",
            "--inference-path",
            "/inference",
        ]
        if shutil.which("ffmpeg"):
            cmd.append("--convert")
        if state.device == "cpu":
            cmd.append("-ng")
        return cmd

    # v0.3.0 takes --device (auto|cpu|cuda[:N]); --gpu is not a flag it knows.
    device = "cpu"
    if state.device == "cuda":
        device = "cuda"
    elif state.device == "auto":
        device = "cuda" if detect_gpu_type() == "nvidia_cuda" else "cpu"
    cmd = [
        args.runtime_binary,
        "serve",
        "--asr-model",
        state.model,
        "--host",
        "127.0.0.1",
        "--port",
        str(state.child_port),
        "--no-ui",
        "--device",
        device,
        # verbose_json segments[] and diarized_json are synthesized from the
        # runtime's words array, so the child must emit word timestamps in JSON.
        "--word-times",
        # The shim decodes the child's body as JSON (and relays the json format
        # as application/json), so the runtime must not answer in its default
        # text format.
        "--format",
        "json",
    ]
    if state.encoder:
        # v0.3.0 names the encoder sidecar --salm-encoder.
        cmd.extend(["--salm-encoder", state.encoder])
    if state.instruction:
        cmd.extend(["--instruction", state.instruction])
    if state.translate_to:
        cmd.extend(["--translate-to", state.translate_to])
    if state.diar_model:
        cmd.extend(["--diar-model", state.diar_model])
    if state.diarize:
        cmd.append("--diarize")
    return cmd


def _ready_url() -> str:
    """The child's own readiness route (both runtimes expose one)."""
    if state.model_type == "whispercpp":
        return f"http://127.0.0.1:{state.child_port}/health"
    return f"http://127.0.0.1:{state.child_port}/ready"


async def _poll_child_ready(child: subprocess.Popen, timeout_s: float) -> bool:
    """Poll the child's readiness route until it answers 200 or the child dies.

    The child's startup banner is NOT usable: whisper-server prints it with
    ``printf`` and no flush, so it stays block-buffered on the pipe. Its own
    health route is the authoritative signal.
    """
    deadline = time.monotonic() + timeout_s
    url = _ready_url()
    assert state.session is not None
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        try:
            async with state.session.get(
                url, timeout=aiohttp.ClientTimeout(total=2)
            ) as response:
                if response.status == 200:
                    return True
        except (TimeoutError, aiohttp.ClientError):
            pass
        await asyncio.sleep(0.5)
    return False


def _tail() -> str:
    """The last few non-empty child lines, for a failure message."""
    lines = [line for line in state.child_tail if line.strip()]
    return " | ".join(lines[-5:])


async def _wait_for_child_ready(child: subprocess.Popen) -> None:
    """Background task: mark the child ready, or record why it never was."""
    try:
        ready = await _poll_child_ready(child, state.runtime_timeout_s)
        if ready and child.poll() is None:
            state.child_ready = True
            print("[READY] runtime ready", flush=True)
            return
        if child.poll() is not None:
            state.child_error = (
                f"runtime exited with code {child.returncode} before becoming "
                f"ready: {_tail()}"
            )
        else:
            state.child_error = (
                f"runtime did not become ready within {state.runtime_timeout_s:g}s"
            )
        print(f"[ERROR] {state.child_error}", flush=True)
    except Exception as exc:  # noqa: BLE001
        # An unobserved task exception would leave /health at starting forever.
        state.child_error = f"readiness check failed: {exc}"
        print(f"[ERROR] {state.child_error}", flush=True)


async def _watch_child(child: subprocess.Popen) -> None:
    """Shut the shim down when a READY runtime dies.

    A child that dies before readiness is a different case: /health must report
    ``runtime_failed`` with the child's output so the operator can read why, so the
    shim stays up for that.
    """
    while not state.child_ready:
        if child.poll() is not None:
            return
        await asyncio.sleep(0.5)
    while child.poll() is None:
        await asyncio.sleep(1)
    if state.child_error is None:
        state.child_error = f"runtime exited with code {child.returncode}"
        print(f"[ERROR] {state.child_error}", flush=True)
    # The host observes the SHIM exit, which is what fails the instance.
    os.kill(os.getpid(), signal.SIGTERM)


def _reader(child: subprocess.Popen) -> None:
    """Forward the child's stdout and keep its tail for failure messages."""
    assert child.stdout is not None
    for line in child.stdout:
        print(line, end="", flush=True)
        state.child_tail.append(line.rstrip("\n"))


def _terminate(child: subprocess.Popen) -> None:
    """Stop the child: SIGTERM, wait, then SIGKILL."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Spawn the runtime, then answer /health while it warms up."""
    if state.runtime_timeout_s <= 0:
        state.runtime_timeout_s = 600.0
    state.child_port = _free_loopback_port()
    cmd = _child_command(_ARGS)
    logger.info("Starting runtime: %s", " ".join(cmd))
    # Popen only forks and execs (it does not wait), and the reader thread is
    # started after it, so the preexec hook runs with no thread of ours alive.
    child = subprocess.Popen(  # noqa: ASYNC220
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
        # PR_SET_PDEATHSIG keeps the runtime from outliving the shim.
        preexec_fn=(  # noqa: PLW1509
            _pdeathsig if sys.platform.startswith("linux") else None
        ),
    )
    state.child = child
    threading.Thread(target=_reader, args=(child,), daemon=True).start()
    state.session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=state.request_timeout_s, sock_connect=10)
    )
    # Do NOT wait for the child here: uvicorn binds the listening socket only
    # after the lifespan returns, so waiting would make /health unreachable
    # (a connection refusal, never a 503).
    state.ready_task = asyncio.create_task(_wait_for_child_ready(child))
    state.ready_task.add_done_callback(_ready_done)
    state.watch_task = asyncio.create_task(_watch_child(child))
    state.watch_task.add_done_callback(_ready_done)
    try:
        yield
    finally:
        if state.ready_task is not None:
            state.ready_task.cancel()
        if state.watch_task is not None:
            state.watch_task.cancel()
        _terminate(child)
        if state.session is not None:
            await state.session.close()


def _ready_done(task: asyncio.Task) -> None:
    """Keep the readiness task's exception from being reported as unobserved."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Readiness task failed: %s", exc)


app = FastAPI(
    title="Audio Shim Server",
    description="OpenAI-compatible transcription for GGML speech runtimes",
    version="1.0.0",
    lifespan=lifespan,
)


async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Verify the host API key, mirroring hf_server's check."""
    if not state.api_key:
        return True
    if credentials and credentials.credentials == state.api_key:
        return True
    api_key = request.headers.get("X-API-Key")
    if api_key and api_key == state.api_key:
        return True
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _error(message: str, code: str, status_code: int = 400) -> JSONResponse:
    """OpenAI-shaped error envelope."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": code,
            }
        },
    )


def _runtime_error(message: str, code: str, status_code: int = 503) -> JSONResponse:
    """OpenAI-shaped runtime error envelope (503 is retryable for control)."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "server_error",
                "param": None,
                "code": code,
            }
        },
    )


# whisper-server answers an undecodable upload with a bare {"error": "..."}
# body and status 500 (see examples/server/server.cpp); that is neither the
# OpenAI envelope nor a client-error status. These are its decode-failure messages.
_DECODE_FAILURE_MARKERS: tuple[str, ...] = (
    "FFmpeg conversion failed",
    "Failed to execute ffmpeg command",
    "failed to read WAV file",
    "failed to read audio data",
)


def _child_error(body: bytes) -> str | None:
    """The child's error string from a bare {"error": "..."} body."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error if isinstance(error, str) else None


@app.get("/health")
async def health():
    """Health check; 503 while the runtime cannot serve."""
    if state.child_error is not None:
        return _runtime_error(f"runtime failed: {state.child_error}", "runtime_failed")
    if not state.child_ready or state.child is None or state.child.poll() is not None:
        if state.child is not None and state.child.poll() is not None:
            state.child_error = f"runtime exited with code {state.child.returncode}"
        return _runtime_error("runtime is starting", "runtime_not_ready")
    return {
        "status": "healthy",
        "model": state.alias,
        "model_type": state.model_type,
    }


@app.get("/v1/models")
async def list_models(_: bool = Depends(verify_api_key)):
    """List the served model with its transcription capability."""
    capabilities = ["transcription"]
    return {
        "models": [
            {
                "name": state.alias,
                "model": state.alias,
                "modified_at": "",
                "size": "",
                "digest": "",
                "type": "model",
                "description": "",
                "tags": [""],
                "capabilities": capabilities,
                "parameters": "",
                "details": {
                    "parent_model": "",
                    "format": "ggml",
                    "family": "",
                    "families": [""],
                    "parameter_size": "",
                    "quantization_level": "",
                },
            }
        ],
        "object": "list",
        "data": [
            {
                "id": state.alias,
                "object": "model",
                "created": int(time.time()),
                "owned_by": state.model_type,
                "capabilities": capabilities,
            }
        ],
    }


def _segments_from_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group NeMo words into segments.

    Consecutive words with the same speaker stay together, split on a pause of
    0.5 s or more and at 30 words. NeMo has no segment-level times, so the
    segments are a host-side grouping of its word timestamps.
    """
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for word in words:
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") or start)
        text = str(word.get("word") or "").strip()
        if not text:
            continue
        speaker = word.get("speaker")
        if (
            current is None
            or current.get("speaker") != speaker
            or start - float(current["end"]) >= 0.5
            or len(current["words"]) >= 30
        ):
            current = {
                "id": len(segments),
                "start": start,
                "end": end,
                "text": text,
                "speaker": speaker,
                "words": [word],
            }
            segments.append(current)
        else:
            current["end"] = max(float(current["end"]), end)
            current["text"] = f"{current['text']} {text}"
            current["words"].append(word)
    return segments


_RE_SPEAKER_LETTER = re.compile(r"^[A-Z]$")


def _speaker_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA: enough labels for any real diarization."""
    label = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def _speaker_labels(speaker_ids: list[str | None]) -> dict[str, str]:
    """Runtime speaker ids -> capital letters in first-appearance order.

    A runtime that already labels speakers with capital letters is left alone;
    otherwise each distinct id gets the next letter. The runtime id itself has no
    place in OpenAI's ``diarized_json`` body, so it is dropped here.
    """
    unique = list(dict.fromkeys(speaker for speaker in speaker_ids if speaker))
    if unique and all(_RE_SPEAKER_LETTER.match(speaker) for speaker in unique):
        return {speaker: speaker for speaker in unique}
    return {speaker: _speaker_letter(index) for index, speaker in enumerate(unique)}


def _diarized_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the OpenAI ``diarized_json`` shape from NeMo's verbose payload."""
    words = payload.get("words") or []
    segments = _diarized_segments(words)
    speakers: list[dict[str, str]] = []
    seen: set[str] = set()
    for segment in segments:
        speaker = segment.get("speaker")
        if speaker and speaker not in seen:
            seen.add(speaker)
            speakers.append({"id": speaker, "name": speaker})
    body: dict[str, Any] = {
        "task": "transcribe",
        "duration": payload.get("duration"),
        "text": payload.get("text") or "",
        "segments": [
            {
                "type": "transcript.text.segment",
                "id": f"seg_{segment['id']}",
                "start": segment["start"],
                "end": segment["end"],
                "text": segment["text"],
                **({"speaker": segment["speaker"]} if segment.get("speaker") else {}),
            }
            for segment in segments
        ],
    }
    if speakers:
        body["speakers"] = speakers
    return body


def _diarized_segments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """NeMo words -> diarized_json segments with capital-letter speakers."""
    segments = _segments_from_words(words)
    labels = _speaker_labels([segment.get("speaker") for segment in segments])
    return [
        {
            "id": index,
            "start": segment["start"],
            "end": segment["end"],
            "text": segment["text"],
            "speaker": labels.get(segment.get("speaker")),
        }
        for index, segment in enumerate(segments)
    ]


def _forward_fields(
    fields: list[tuple[str, str]], response_format: str
) -> list[tuple[str, str]]:
    """The child's fields: drop model, apply the format/diarization matrix."""
    values = dict(fields)
    forwarded: list[tuple[str, str]] = []
    for name, value in fields:
        if name in ("model", "stream"):
            # NeMo treats an unknown model as a 404, whisper-server ignores it;
            # the shim validated it already. stream is not implemented.
            continue
        if name == "language" and not value.strip():
            # An empty language is absent, not an explicit choice: a one-entry
            # languages hint below supplies the runtime's language instead.
            continue
        if name == "temperature" and state.model_type == "nemospeech":
            # The shim accepts temperature=0 as a no-op for NeMo-Speech.cpp and
            # refuses anything else; the runtime has no temperature, so the field
            # must not be forwarded even when it is zero.
            continue
        if name == "vad":
            # No VAD model is configurable on a shim instance. Forwarding vad
            # makes whisper-server try to load an empty model path and fail with
            # a bare 500, so the endpoint refuses vad=true and accepts false as
            # a no-op.
            continue
        if name in OPENAI_OPTIONAL_FIELDS or name in OPENAI_ARRAY_FIELDS:
            # keywords and languages are mapped onto prompt/language below;
            # chunking_strategy and include[] have no runtime counterpart.
            continue
        if name in WHISPER_ONLY_FIELDS and state.model_type != "whispercpp":
            continue
        if name in NEMO_ONLY_FIELDS and state.model_type != "nemospeech":
            continue
        if (
            name == "response_format"
            and state.model_type == "nemospeech"
            and value == "diarized_json"
        ):
            # nemo-speech has no diarized_json format; the shim synthesizes
            # that body from verbose_json plus diarization=true.
            forwarded.append((name, "verbose_json"))
            continue
        if name in ("timestamp_granularities[]", "timestamp_granularities") and (
            state.model_type != "nemospeech"
            or response_format not in ("verbose_json", "diarized_json")
        ):
            # Granularities require verbose_json upstream; the other formats
            # cannot carry them. whisper-server has no such field and always
            # emits per-segment words in verbose_json, so dropping it there is
            # a no-op rather than a silent downgrade.
            continue
        forwarded.append((name, value))

    # A one-entry languages set is a language hint; every runtime takes exactly
    # one language. An explicit non-blank language always wins, and a candidate
    # set of two or more is dropped because no runtime accepts a set.
    if not values.get("language", "").strip():
        language = single_language(
            value for name, value in fields if name in ("languages", "languages[]")
        )
        if language is not None:
            forwarded.append(("language", language))

    # keywords are decoder biasing, which every runtime already takes as a
    # prompt: whisper.cpp and HuggingFace read `prompt`, NeMo-Speech.cpp reads
    # `speech_contexts`. An explicit prompt (or explicit speech_contexts on
    # NeMo) always wins.
    keyword_parts = [
        value for name, value in fields if name in ("keywords", "keywords[]")
    ]
    keyword_prompt = keywords_prompt(keyword_parts)
    if keyword_prompt is not None and "prompt" not in values:
        if state.model_type == "nemospeech":
            if "speech_contexts" not in values:
                forwarded.append(
                    (
                        "speech_contexts",
                        speech_contexts_json(collect_list(keyword_parts)),
                    )
                )
        else:
            forwarded.append(("prompt", keyword_prompt))

    if state.model_type == "nemospeech":
        # An instance-level language is the default for requests that omit it,
        # and the runtime echoes the effective language in verbose_json.
        if state.language and not any(name == "language" for name, _ in forwarded):
            forwarded.append(("language", state.language))
        wants_speakers = response_format in ("verbose_json", "diarized_json")
        if (
            wants_speakers
            and (state.diarize or response_format == "diarized_json")
            and not any(name == "diarization" for name, _ in forwarded)
        ):
            # An explicit diarization field is the caller's choice; appending a
            # second one would send the child two conflicting parts.
            forwarded.append(("diarization", "true"))
    return forwarded


async def _forward(
    upload: UploadFile,
    fields: list[tuple[str, str]],
) -> tuple[int, bytes, str]:
    """Send the multipart to the child runtime and return its raw answer.

    ``fields`` is already the final set: the endpoint applied the format matrix
    and dropped what the child cannot take.

    aiohttp reads a user-supplied file object synchronously in the event loop
    while streaming, so the spooled upload is copied to a disk temp file in a
    worker thread first and the post streams a fresh handle to that copy. A
    fresh handle per request keeps the request retryable: aiohttp closes a
    user-supplied handle after a connection error.
    """
    assert state.session is not None
    if state.model_type == "whispercpp":
        url = f"http://127.0.0.1:{state.child_port}/inference"
    else:
        url = f"http://127.0.0.1:{state.child_port}/v1/audio/transcriptions"

    with tempfile.NamedTemporaryFile(delete=False) as temp:
        temp_path = temp.name
        try:
            upload.file.seek(0)
            await asyncio.to_thread(shutil.copyfileobj, upload.file, temp, 1024 * 1024)
        except BaseException:
            # The outer finally below owns the unlink for a successful copy; a
            # failed seek/copy never reaches it, so remove the temp file here
            # and let the failure surface.
            os.unlink(temp_path)
            raise
    try:
        form = aiohttp.FormData()
        for name, value in fields:
            form.add_field(name, value)
        handle = await asyncio.to_thread(open, temp_path, "rb")
        try:
            form.add_field(
                "file",
                handle,
                filename=upload.filename or "audio",
                content_type=upload.content_type or "application/octet-stream",
            )
            async with state.session.post(url, data=form) as response:
                body = await response.read()
                content_type = response.headers.get(
                    "Content-Type", "application/octet-stream"
                )
                return response.status, body, content_type
        finally:
            handle.close()
    finally:
        os.unlink(temp_path)


def _wave_format(upload: UploadFile) -> WaveFormat | str | None:
    """Parse the RIFF/WAVE ``fmt `` chunk, or None when the upload is not WAV.

    A ``data`` chunk before the ``fmt `` chunk returns the distinct chunk-order
    problem string instead: the shim requires the ``fmt `` chunk first, and the
    unreadable-file message would wrongly describe a valid RIFF/WAVE upload.

    The magic alone is not enough: the NeMo-Speech model requires 16 kHz mono
    PCM16, and wider input was observed to mis-transcribe, so the sample rate,
    channel count, bit depth and encoding are read from the header the runtime
    will actually use.
    """
    upload.file.seek(0)
    try:
        header = upload.file.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        while True:
            chunk_header = upload.file.read(8)
            if len(chunk_header) < 8:
                return None
            chunk_id = chunk_header[:4]
            size = int.from_bytes(chunk_header[4:8], "little")
            if chunk_id == b"data":
                # The shim requires the fmt chunk first; a data-first WAV is a
                # valid RIFF layout but an unsupported chunk order here, so stop
                # the walk and name the order instead of scanning on to EOF and
                # reporting the file as unreadable.
                return _WAVE_DATA_BEFORE_FMT_PROBLEM
            if chunk_id == b"fmt ":
                # The parser reads at most 40 bytes of the chunk: the largest
                # layout it understands is a WAVEFORMATEXTENSIBLE header, and a
                # crafted size must never turn into a huge read.
                data = upload.file.read(min(size, 40))
                if len(data) < 16:
                    return None
                audio_format = int.from_bytes(data[0:2], "little")
                if audio_format == 0xFFFE:
                    # WAVE_FORMAT_EXTENSIBLE names only the wrapper; the real
                    # encoding is the first two bytes of its SubFormat GUID.
                    if len(data) < 26:
                        return None
                    extension_size = int.from_bytes(data[16:18], "little")
                    if extension_size < 22:
                        return None
                    audio_format = int.from_bytes(data[24:26], "little")
                return WaveFormat(
                    audio_format=audio_format,
                    channels=int.from_bytes(data[2:4], "little"),
                    sample_rate=int.from_bytes(data[4:8], "little"),
                    bits_per_sample=int.from_bytes(data[14:16], "little"),
                )
            # Chunks are word-aligned: an odd-sized chunk carries a pad byte.
            upload.file.seek(size + (size & 1), os.SEEK_CUR)
    finally:
        upload.file.seek(0)


def _wave_problem(upload: UploadFile) -> str | None:
    """A legible 400 when the upload is not the WAV the model accepts.

    The message names what was received and what the model requires, because the
    magic-only guard accepted a 44.1 kHz stereo upload and mis-transcribed it
    while the refusal claimed 16 kHz mono PCM16 only. The runtime's own upload
    contract is wider (mono or stereo PCM16 and float32 WAVs at 8-96 kHz,
    downmixing stereo), but wider input was observed to mis-transcribe, so the
    shim deliberately keeps this strict gate and refuses it.
    """
    wave = _wave_format(upload)
    if isinstance(wave, str):
        return wave
    if wave is None:
        return (
            "NeMo-Speech.cpp accepts uncompressed WAV only: the upload is not a "
            "readable RIFF/WAVE file. Convert it to 16 kHz mono PCM16 WAV with: "
            "ffmpeg -i input -ar 16000 -ac 1 -c:a pcm_s16le output.wav"
        )
    problems: list[str] = []
    if wave.audio_format != 1:
        name = _WAVE_FORMAT_NAMES.get(
            wave.audio_format, f"format code {wave.audio_format}"
        )
        problems.append(f"{name} encoding")
    if wave.sample_rate != 16000:
        problems.append(f"{wave.sample_rate} Hz")
    if wave.channels != 1:
        problems.append(f"{wave.channels} channels")
    if wave.bits_per_sample != 16:
        problems.append(f"{wave.bits_per_sample}-bit samples")
    if not problems:
        return None
    received = (
        f"{wave.sample_rate} Hz, {wave.channels} channel(s), "
        f"{wave.bits_per_sample}-bit "
        f"{_WAVE_FORMAT_NAMES.get(wave.audio_format, 'unknown encoding')}"
    )
    return (
        f"NeMo-Speech.cpp accepts uncompressed WAV only: it requires "
        f"{_NEMO_WAVE_REQUIREMENTS}, but the upload is {received}. Convert "
        f"it with: ffmpeg -i input -ar 16000 -ac 1 -c:a pcm_s16le output.wav"
    )


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    _: bool = Depends(verify_api_key),
):
    """OpenAI-compatible transcription, forwarded to the child runtime."""
    started = time.monotonic()

    if state.child_error is not None:
        return _runtime_error(f"runtime failed: {state.child_error}", "runtime_failed")
    if not state.child_ready or state.child is None or state.child.poll() is not None:
        return _runtime_error("runtime is starting", "runtime_not_ready")

    try:
        form = await request.form()
    except (MultipartParseError, MultiPartException, StarletteHTTPException):
        # Three layers can surface a bad body: python-multipart's own
        # MultipartParseError, Starlette's MultiPartException, and - because
        # Starlette converts the latter to HTTPException(400) when the request
        # runs inside an app - an HTTPException. None of them carries the OpenAI
        # envelope, so all are answered with one here.
        return _error("malformed multipart body", "invalid_request_error")

    try:
        fields: list[tuple[str, str]] = []
        upload: UploadFile | None = None
        for name, value in form.multi_items():
            if name == "file":
                if isinstance(value, UploadFile):
                    upload = value
                continue
            if isinstance(value, str):
                fields.append((name, value))

        values: dict[str, str] = dict(fields)
        response_format = values.get("response_format", "json")

        if values.get("stream", "false").lower() in ("1", "true", "yes"):
            return _error(
                "streaming transcription is not supported by this endpoint; "
                "omit 'stream' or set it to false",
                "invalid_request_error",
            )
        if response_format not in RESPONSE_FORMATS:
            return _error(
                f"response_format '{response_format}' is not supported; must be one "
                f"of: {', '.join(RESPONSE_FORMATS)}",
                "invalid_request_error",
            )
        if response_format == "diarized_json" and state.model_type != "nemospeech":
            runtime = (
                "whisper.cpp" if state.model_type == "whispercpp" else "HuggingFace"
            )
            return _error(
                f"response_format 'diarized_json' is not supported by the {runtime} "
                f"audio backend; supported formats are json, text, srt, verbose_json, vtt",
                "invalid_request_error",
            )
        if (
            response_format == "diarized_json"
            and state.model_type == "nemospeech"
            and not state.diar_model
        ):
            return _error(
                "response_format 'diarized_json' requires a diarizer model; start the "
                "instance with diar_model set to a Sortformer GGUF",
                "invalid_request_error",
            )
        if state.model_type == "whispercpp" and values.get("vad", "false").lower() in (
            "1",
            "true",
            "yes",
        ):
            return _error(
                "vad requires a VAD model, and no VAD model is configured for this "
                "whisper.cpp instance; omit vad or set it to false",
                "invalid_request_error",
            )
        for name in values:
            if name not in ACCEPTED_FIELDS:
                return _error(
                    f"field '{name}' is not supported by /v1/audio/transcriptions",
                    "invalid_request_error",
                )
        # NeMo-Speech.cpp receives this field, so an unknown entry must be refused
        # here rather than at the runtime, which cannot know the OpenAI vocabulary.
        for name, value in fields:
            if (
                name in ("timestamp_granularities[]", "timestamp_granularities")
                and value not in TIMESTAMP_GRANULARITIES
            ):
                return _error(
                    "timestamp_granularities must contain only 'word' or 'segment'",
                    "invalid_request_error",
                )
        if upload is None:
            return _error("file is required", "invalid_request_error")
        if "model" not in values:
            return _error("model is required", "invalid_request_error")
        if values["model"] != state.alias:
            return _error(
                f"model '{values['model']}' is not served by this instance; "
                f"it serves '{state.alias}'",
                "model_not_found",
                404,
            )
        if state.model_type == "nemospeech":
            # The header walk is bounded only by the upload size: a crafted WAV of
            # zero-length chunks costs one read per 8 bytes, so the inspection
            # must not run on the event loop.
            wave_problem = await asyncio.to_thread(_wave_problem, upload)
            if wave_problem is not None:
                return _error(wave_problem, "invalid_request_error")
            temperature = values.get("temperature")
            if temperature is not None:
                try:
                    requested = float(temperature)
                except ValueError:
                    return _error(
                        f"temperature '{temperature}' is not a number",
                        "invalid_request_error",
                    )
                if requested != 0.0:
                    return _error(
                        "temperature is not supported by NeMo-Speech.cpp "
                        "transcriptions; omit it or set it to 0",
                        "invalid_request_error",
                    )
        else:
            wave_format = await asyncio.to_thread(_wave_format, upload)
            if wave_format is None and not shutil.which("ffmpeg"):
                return _error(
                    f"whisper.cpp cannot convert '{upload.filename}': ffmpeg is not "
                    f"installed on the host; upload a WAV file",
                    "invalid_request_error",
                )

        forwarded = _forward_fields(fields, response_format)
        print(
            f"[REQUEST] model={state.alias} endpoint=/v1/audio/transcriptions "
            f"format={response_format}",
            flush=True,
        )
        try:
            status, body, content_type = await _forward(upload, forwarded)
        except (TimeoutError, aiohttp.ClientError) as exc:
            print(f"[ERROR] runtime request failed: {exc}", flush=True)
            return _runtime_error(
                f"runtime request failed: {exc}", "runtime_unreachable"
            )
    finally:
        # FormData.close() closes every UploadFile part. Starlette keeps only
        # the last value per key, so closing the selected ``upload`` alone would
        # leak the earlier ``file`` parts' spooled temp files until GC.
        await form.close()

    elapsed = time.monotonic() - started
    if status != 200:
        text = body.decode("utf-8", "replace")
        print(f"[ERROR] runtime answered {status}: {text[:200]}", flush=True)
        if state.model_type == "whispercpp":
            child_error = _child_error(body)
            if child_error is not None and any(
                marker in child_error for marker in _DECODE_FAILURE_MARKERS
            ):
                return _error(
                    f"could not decode the uploaded audio: {child_error} "
                    "The file is empty or not an audio format the runtime can "
                    "decode; convert it to 16 kHz mono PCM16 WAV and retry.",
                    "invalid_request_error",
                )
        return Response(content=body, status_code=status, media_type=content_type)

    print(
        f"[COMPLETE] model={state.alias} elapsed_s={elapsed:.2f} "
        f"time_ms={elapsed * 1000:.0f}",
        flush=True,
    )

    if response_format == "text":
        return PlainTextResponse(body.decode("utf-8", "replace"))
    if state.model_type == "whispercpp":
        if response_format == "verbose_json":
            try:
                payload = json.loads(body)
            except ValueError:
                return Response(content=body, media_type=content_type)
            # whisper.cpp reports the detected language as a full name; the
            # OpenAI contract wants the ISO-639-1 code.
            payload["language"] = normalize_language(payload.get("language"))
            return JSONResponse(payload)
        return Response(content=body, media_type=content_type)
    if response_format in ("srt", "vtt"):
        # NeMo's own route answers both as text/plain, while the other two
        # runtimes serve the subtitle media types. A client selects the format
        # by the media type, so all three must agree.
        return Response(content=body, media_type=FORMAT_MEDIA_TYPES[response_format])
    if response_format == "json":
        return Response(content=body, media_type=FORMAT_MEDIA_TYPES["json"])

    try:
        payload = json.loads(body)
    except ValueError:
        return Response(content=body, media_type=content_type)
    if response_format == "verbose_json":
        # NeMo reports "" when no language was requested; the OpenAI contract
        # wants "und" for an undetected language.
        payload["language"] = normalize_language(payload.get("language"))
        words = payload.get("words") or []
        if words and "segments" not in payload:
            payload["segments"] = _segments_from_words(words)
        return JSONResponse(payload)
    # diarized_json
    return JSONResponse(_diarized_json(payload))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audio shim server")
    parser.add_argument(
        "--model-type",
        choices=["whispercpp", "nemospeech"],
        required=True,
    )
    parser.add_argument("--runtime-binary", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--language", default=None)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--encoder", default=None)
    parser.add_argument("--instruction", default=None)
    parser.add_argument("--translate-to", default=None)
    parser.add_argument("--diar-model", default=None)
    parser.add_argument("--diarize", action="store_true")
    parser.add_argument("--runtime-timeout-s", type=float, default=600.0)
    parser.add_argument("--request-timeout-s", type=float, default=1800.0)
    parser.add_argument(
        "--api-key",
        default="",
        help="API key (deprecated: prefer the SOLAR_API_KEY env var)",
    )
    return parser.parse_args(argv)


def main() -> None:
    global _ARGS
    _ARGS = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    state.alias = _ARGS.alias
    state.model_type = _ARGS.model_type
    state.model = _ARGS.model
    state.language = _ARGS.language
    state.device = _ARGS.device
    state.encoder = _ARGS.encoder
    state.instruction = _ARGS.instruction
    state.translate_to = _ARGS.translate_to
    state.diar_model = _ARGS.diar_model
    state.diarize = _ARGS.diarize
    state.runtime_timeout_s = _ARGS.runtime_timeout_s
    state.request_timeout_s = _ARGS.request_timeout_s
    state.api_key = resolve_api_key(_ARGS.api_key, os.environ.get("SOLAR_API_KEY", ""))

    uvicorn.run(app, host=_ARGS.host, port=_ARGS.port, log_level="info")


if __name__ == "__main__":
    main()
