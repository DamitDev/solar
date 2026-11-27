#!/usr/bin/env python3
"""Fake whisper-server for the audio shim process tests.

Mimics the real binary's contract that matters here: it binds ``--port``, answers
``GET /health`` with 503 while loading and 200 once ready, and answers
``POST /inference`` with the canned body for the requested ``response_format``.
The startup banner is printed to stdout WITHOUT a flush, exactly like the real
``printf``, so a shim that parsed the banner instead of polling /health would hang.

Environment knobs:
    FAKE_RUNTIME_BANNER_DELAY_S <seconds>  delay before the banner (default 0)
    FAKE_RUNTIME_NEVER_READY=1             never answer /health with 200
    FAKE_RUNTIME_EXIT_IMMEDIATELY=1         print one line and exit non-zero
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

BANNER_DELAY_S = float(os.environ.get("FAKE_RUNTIME_BANNER_DELAY_S", "0"))
NEVER_READY = os.environ.get("FAKE_RUNTIME_NEVER_READY") == "1"
EXIT_IMMEDIATELY = os.environ.get("FAKE_RUNTIME_EXIT_IMMEDIATELY") == "1"

READY = threading.Event()

BODIES = {
    "json": (b'{"text": "hello world"}', "application/json"),
    "text": (b"hello world", "text/html; charset=utf-8"),
    "srt": (
        b"1\n00:00:00,000 --> 00:00:01,000\nhello world\n",
        "application/x-subrip",
    ),
    "vtt": (b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello world\n", "text/vtt"),
    "verbose_json": (
        json.dumps(
            {
                "task": "transcribe",
                "language": "en",
                "duration": 1.0,
                "text": "hello world",
                "segments": [
                    {
                        "id": 0,
                        "start": 0.0,
                        "end": 1.0,
                        "text": "hello world",
                        "tokens": [1, 2],
                        "avg_logprob": -0.1,
                        "no_speech_prob": 0.01,
                    }
                ],
            }
        ).encode(),
        "application/json",
    ),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--model", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("-t", "--threads", default="4")
    parser.add_argument("-l", "--language", default="en")
    parser.add_argument("--inference-path", default="/inference")
    parser.add_argument("--convert", action="store_true")
    parser.add_argument("-ng", "--no-gpu", action="store_true")
    parser.add_argument("--api-key", default="")
    return parser.parse_known_args()[0]


ARGS = _parse_args()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout clean for the shim's parser
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            if READY.is_set():
                self._send(200, b'{"status": "ok"}', "application/json")
            else:
                self._send(503, b'{"status": "loading model"}', "application/json")
            return
        self._send(404, b"{}", "application/json")

    def do_POST(self):
        if not self.path.endswith("/inference"):
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
                value = rest[value_start + 4 :]
                fmt = value.split("\r\n", 1)[0].strip() or "json"
        payload, content_type = BODIES.get(fmt, BODIES["json"])
        self._send(200, payload, content_type)


def main() -> None:
    if EXIT_IMMEDIATELY:
        print("fake runtime: starting", flush=True)
        time.sleep(0.2)
        sys.exit(3)

    if BANNER_DELAY_S > 0:
        time.sleep(BANNER_DELAY_S)
    # No flush: a shim that parses the banner instead of polling /health hangs.
    print(f"whisper server listening at http://{ARGS.host}:{ARGS.port}", end="\n")
    if not NEVER_READY:
        READY.set()

    server = HTTPServer((ARGS.host, ARGS.port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
