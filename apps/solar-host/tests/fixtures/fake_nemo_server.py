#!/usr/bin/env python3
"""Fake ``nemo-speech serve`` for the audio shim process tests.

Binds ``--port``, answers ``GET /ready`` with 503 while loading and 200 once
ready, and answers ``POST /v1/audio/transcriptions`` with a NeMo-shaped body.

Environment knobs:
    FAKE_RUNTIME_NEVER_READY=1       never answer /ready with 200
    FAKE_RUNTIME_EXIT_IMMEDIATELY=1  print one line and exit non-zero
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

NEVER_READY = os.environ.get("FAKE_RUNTIME_NEVER_READY") == "1"
EXIT_IMMEDIATELY = os.environ.get("FAKE_RUNTIME_EXIT_IMMEDIATELY") == "1"

READY = threading.Event()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?")
    parser.add_argument("--asr-model", "--asr.model.path", dest="model", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--gpu", default="0")
    parser.add_argument("--diar-model", "--asr.diar.model_path", default="")
    parser.add_argument("--asr.salm.encoder_path", default="")
    return parser.parse_known_args()[0]


ARGS = _parse_args()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ready":
            if READY.is_set():
                self._send(200, b'{"ready": true}', "application/json")
            else:
                self._send(503, b'{"ready": false}', "application/json")
            return
        if self.path == "/health":
            self._send(200, b'{"status": "ok"}', "application/json")
            return
        self._send(404, b"{}", "application/json")

    def do_POST(self):
        if not self.path.endswith("/v1/audio/transcriptions"):
            self._send(404, b"{}", "application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        text = body.decode("latin-1")
        fmt = "json"
        marker = 'name="response_format"'
        if marker in text:
            rest = text.split(marker, 1)[1]
            value_start = rest.find("\r\n\r\n")
            if value_start != -1:
                fmt = rest[value_start + 4 :].split("\r\n", 1)[0].strip() or "json"
        if fmt == "verbose_json":
            payload = {
                "text": "hello world",
                "task": "transcribe",
                "language": "en",
                "duration": 1.0,
                "words": [
                    {"word": "hello", "start": 0.0, "end": 0.5},
                    {"word": "world", "start": 0.5, "end": 1.0},
                ],
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if fmt == "text":
            self._send(200, b"hello world", "text/plain; charset=utf-8")
            return
        self._send(200, b'{"text": "hello world"}', "application/json")


def main() -> None:
    if EXIT_IMMEDIATELY:
        print("fake nemo runtime: starting", flush=True)
        time.sleep(0.2)
        sys.exit(3)

    print(f"nemo-speech serving on http://{ARGS.host}:{ARGS.port}", flush=True)
    if not NEVER_READY:
        READY.set()

    server = HTTPServer((ARGS.host, ARGS.port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
