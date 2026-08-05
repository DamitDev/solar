"""Stub Harbor — OCI Distribution v2 HTTP server for integration tests.

Speaks just enough of the OCI Distribution spec for BOTH real clients used
by the platform:

- ``harbor_oci_client.HarborClient.verify_artifact`` (data-repository):
  ``GET /service/token`` (Basic auth) -> bearer token, then
  ``HEAD /v2/{repo}/manifests/{ref}`` with ``Accept`` + ``Docker-Content-Digest``.
- ``harbor_oci_client.OrasHelper`` (solar-host, wraps oras-py with the
  ``token`` auth backend): unauthenticated request -> ``401`` with
  ``Www-Authenticate: Bearer realm=...`` -> token fetch (Basic auth) ->
  retry with bearer token. Then ``GET /v2/{repo}/manifests/{ref}`` and
  ``GET /v2/{repo}/blobs/{digest}``.

The stub runs in a background thread (``ThreadingHTTPServer``) on a random
loopback port. It records every request it receives so tests can assert
*who* called Harbor and how often (``received_requests()``).

Artifacts are registered with ``register_model(ref, files)`` where ``files``
is ``{filename: bytes}``. Each file becomes a flat OCI layer with
``org.opencontainers.image.title`` set to the filename and a real
``sha256:`` digest of the file bytes — exactly what ``OrasHelper.pull``
writes to disk (one file per layer, no tarball).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"  # NOT +gzip (direct write)
CONFIG_MEDIA_TYPE = "application/vnd.unknown.config.v1+json"
TITLE_ANNOTATION = "org.opencontainers.image.title"

# Blank config blob ("{}"): digest + size known from the OCI spec.
_BLANK_CONFIG_DIGEST = (
    "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
)
_BLANK_CONFIG_SIZE = 2

_TOKEN = "stub-harbor-test-token"
_SERVICE = "harbor-registry"

# repo ref path pattern: /v2/{repository}/manifests|blobs/{reference}
_PATH_RE = re.compile(
    r"^/v2/(?P<repo>[^/]+(?:/[^/]+)*)/(?P<kind>manifests|blobs)/(?P<ref>[^/]+)$"
)
# blob upload session paths (S-047 write path)
_UPLOAD_POST_RE = re.compile(r"^/v2/(?P<repo>[^/]+(?:/[^/]+)*)/blobs/uploads/$")
_UPLOAD_PATH_RE = re.compile(
    r"^/v2/(?P<repo>[^/]+(?:/[^/]+)*)/blobs/uploads/(?P<uuid>[^/?]+)$"
)
# Harbor v2.0 REST API artifact delete (rollback path)
_V2_DELETE_RE = re.compile(
    r"^/api/v2\.0/projects/(?P<project>[^/]+)/repositories/"
    r"(?P<repo>[^/]+)/artifacts/(?P<ref>[^/]+)$"
)
# Harbor v2.0 REST API repository delete (S-048 best-effort cleanup)
_V2_REPO_DELETE_RE = re.compile(
    r"^/api/v2\.0/projects/(?P<project>[^/]+)/repositories/(?P<repo>[^/]+)$"
)


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _StubHarborState:
    """Mutable state shared with the request handler."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # repo -> {reference: manifest_dict}
        self.manifests: dict[str, dict[str, dict[str, Any]]] = {}
        # digest -> bytes
        self.blobs: dict[str, bytes] = {}
        # upload uuid -> {repo, buffer} for in-flight blob upload sessions
        self.uploads: dict[str, dict[str, Any]] = {}
        # (method, path, headers-dict) log
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.base_url = ""
        # When True, artifact deletes return 500 (simulates Harbor failure).
        self.reject_artifact_delete = False
        # Optional on-disk mirror of the request log (for debugging hangs).
        self.log_file: str = ""

    def record(self, method: str, path: str, headers: dict[str, str]) -> None:
        with self._lock:
            self.requests.append((method, path, dict(headers)))
        if self.log_file:
            try:
                auth = headers.get("Authorization", "")[:12]
                with open(self.log_file, "a") as f:
                    f.write(f"{method} {path} auth={auth}\n")
            except Exception:  # noqa: BLE001, S110
                pass

    def received_requests(self) -> list[tuple[str, str, dict[str, str]]]:
        with self._lock:
            return list(self.requests)

    def reset(self) -> None:
        with self._lock:
            self.requests = []
            self.reject_artifact_delete = False

    def register_model(
        self, harbor_ref: str, files: dict[str, bytes]
    ) -> dict[str, Any]:
        """Register an artifact. ``harbor_ref`` e.g. ``127.0.0.1:PORT/supernova/test-model:v1``.

        Returns the manifest dict (with its digest under ``_digest``).
        """
        repo, reference = split_ref(harbor_ref)
        layers = []
        for filename, data in files.items():
            digest = sha256_digest(data)
            with self._lock:
                self.blobs[digest] = data
            layers.append(
                {
                    "mediaType": LAYER_MEDIA_TYPE,
                    "digest": digest,
                    "size": len(data),
                    "annotations": {TITLE_ANNOTATION: filename},
                }
            )
        manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST_MEDIA_TYPE,
            "config": {
                "mediaType": CONFIG_MEDIA_TYPE,
                "digest": _BLANK_CONFIG_DIGEST,
                "size": _BLANK_CONFIG_SIZE,
            },
            "layers": layers,
        }
        with self._lock:
            self.blobs[_BLANK_CONFIG_DIGEST] = b"{}"
            self.manifests.setdefault(repo, {})[reference] = manifest
        return manifest

    def get_manifest(self, repo: str, reference: str) -> dict[str, Any] | None:
        with self._lock:
            return self.manifests.get(repo, {}).get(reference)

    def get_blob(self, digest: str) -> bytes | None:
        with self._lock:
            return self.blobs.get(digest)

    def repo_manifest_count(self, repo: str) -> int:
        """Number of manifests still registered under *repo*."""
        with self._lock:
            return len(self.manifests.get(repo, {}))


def split_ref(harbor_ref: str) -> tuple[str, str]:
    """Split ``host/repo:tag`` (or ``host/repo@digest``) into (repo, reference).

    The host part is stripped; the stub serves any host as long as the
    repository+reference path matches.
    """
    # Strip scheme if present
    if "://" in harbor_ref:
        harbor_ref = harbor_ref.split("://", 1)[1]
    rest = harbor_ref.split("/", 1)[1] if "/" in harbor_ref else harbor_ref
    if "@" in rest:
        repo, reference = rest.split("@", 1)
    elif ":" in rest:
        repo, reference = rest.rsplit(":", 1)
    else:
        repo, reference = rest, "latest"
    return repo, reference


class StubHarborHandler(BaseHTTPRequestHandler):
    """Minimal OCI Distribution handler. State lives on ``self.server.state``."""

    protocol_version = "HTTP/1.1"
    server_version = "StubHarbor/1.0"

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    @property
    def state(self) -> _StubHarborState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("stub-harbor: " + format, *args)

    def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(
        self, status: int, obj: Any, extra: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(obj).encode()
        headers = {"Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        self._send(status, body, headers)

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------

    def _has_bearer(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and auth[7:] == _TOKEN

    def _has_basic(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
        except Exception:  # noqa: BLE001
            return False
        # Accept any non-empty user:password (robot-style creds).
        return ":" in decoded and len(decoded) > 1

    def _challenge(self, scope: str) -> None:
        """401 with a Docker-style bearer challenge (oras-py token dance)."""
        realm = f"{self.state.base_url}/service/token"
        self._send(
            401,
            b"",
            {
                "Content-Type": "application/json",
                "Www-Authenticate": (
                    f'Bearer realm="{realm}",service="{_SERVICE}",scope="{scope}"'
                ),
            },
        )

    def _has_sid_cookie(self) -> bool:
        """Behaviour 1 trap: a replayed ``sid`` cookie must fail with 403."""
        return "sid=" in self.headers.get("Cookie", "")

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _dispatch(self, method: str) -> None:
        self.state.record(method, self.path, dict(self.headers))
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path == "/service/token":
            self._handle_token()
            return

        if path == "/v2/":
            # Capability probe — challenge like a real registry.
            self._challenge("registry:catalog:*")
            return

        # Harbor v2.0 REST API (rollback deletes + repository cleanup)
        m = _V2_DELETE_RE.match(path)
        if m:
            self._handle_v2_delete(m)
            return
        m = _V2_REPO_DELETE_RE.match(path)
        if m and method == "DELETE":
            self._handle_v2_repo_delete(m)
            return

        # Blob upload session: open (POST) / PATCH / close (PUT)
        m = _UPLOAD_POST_RE.match(path)
        if m:
            self._handle_upload_open(m)
            return
        m = _UPLOAD_PATH_RE.match(path)
        if m:
            self._handle_upload_patch_or_close(m, method, query)
            return

        m = _PATH_RE.match(path)
        if not m:
            self._send(
                404,
                b'{"errors":[{"code":"UNSUPPORTED","message":"not found"}]}',
                {"Content-Type": "application/json"},
            )
            return

        if not self._has_bearer():
            self._challenge(f"repository:{m.group('repo')}:pull")
            return

        repo, kind, ref = m.group("repo"), m.group("kind"), m.group("ref")

        if kind == "manifests":
            if method == "PUT":
                self._handle_manifest_put(repo, ref)
                return
            manifest = self.state.get_manifest(repo, ref)
            if manifest is None:
                self._send(
                    404,
                    b'{"errors":[{"code":"MANIFEST_UNKNOWN","message":"manifest unknown"}]}',
                    {"Content-Type": "application/json"},
                )
                return
            body = json.dumps(manifest).encode()
            headers = {
                "Content-Type": MANIFEST_MEDIA_TYPE,
                "Docker-Content-Digest": sha256_digest(body),
            }
            self._send(200, body, headers)
            return

        if kind == "blobs":
            blob = self.state.get_blob(ref)
            if blob is None:
                self._send(
                    404,
                    b'{"errors":[{"code":"BLOB_UNKNOWN","message":"blob unknown"}]}',
                    {"Content-Type": "application/json"},
                )
                return
            self._send(200, blob, {"Content-Type": "application/octet-stream"})
            return

        self._send(404, b"", {})

    # ------------------------------------------------------------------
    # blob upload session (S-047 write path)
    # ------------------------------------------------------------------

    def _handle_upload_open(self, m: re.Match[str]) -> None:
        if self._has_sid_cookie():
            self._send(
                403,
                b'{"errors":[{"code":"DENIED","message":"CSRF token invalid"}]}',
                {"Content-Type": "application/json"},
            )
            return
        if not self._has_bearer():
            self._challenge(f"repository:{m.group('repo')}:push")
            return
        upload_id = f"up-{len(self.state.uploads) + 1}"
        with self.state._lock:
            self.state.uploads[upload_id] = {
                "repo": m.group("repo"),
                "buffer": bytearray(),
            }
        location = f"/v2/{m.group('repo')}/blobs/uploads/{upload_id}?_state={upload_id}"
        self._send(
            202,
            b"",
            {"Location": location, "Content-Type": "application/octet-stream"},
        )

    def _handle_upload_patch_or_close(
        self, m: re.Match[str], method: str, query: str
    ) -> None:
        if self._has_sid_cookie():
            self._send(
                403,
                b'{"errors":[{"code":"DENIED","message":"CSRF token invalid"}]}',
                {"Content-Type": "application/json"},
            )
            return
        if not self._has_bearer():
            self._challenge(f"repository:{m.group('repo')}:push")
            return

        upload_id = m.group("uuid")
        with self.state._lock:
            upload = self.state.uploads.get(upload_id)
        if upload is None:
            self._send(
                404,
                b'{"errors":[{"code":"BLOB_UPLOAD_UNKNOWN","message":"upload unknown"}]}',
                {"Content-Type": "application/json"},
            )
            return

        if method == "PATCH":
            upload["buffer"].extend(self._read_body())
            location = (
                f"/v2/{m.group('repo')}/blobs/uploads/{upload_id}?_state={upload_id}"
            )
            self._send(
                202,
                b"",
                {"Location": location, "Content-Type": "application/octet-stream"},
            )
            return

        if method == "PUT":
            # Behaviour 2 trap: closing without the _state query fails.
            if "_state=" not in query:
                self._send(
                    404,
                    b'{"errors":[{"code":"BLOB_UPLOAD_INVALID",'
                    b'"message":"blob upload invalid"}]}',
                    {"Content-Type": "application/json"},
                )
                return
            data = bytes(upload["buffer"])
            computed = sha256_digest(data)
            # Accept digest=sha256:<hex> (with or without the prefix).
            digest_param = query.split("digest=", 1)[1].split("&", 1)[0]
            if digest_param != computed:
                self._send(
                    400,
                    b'{"errors":[{"code":"DIGEST_INVALID","message":"digest invalid"}]}',
                    {"Content-Type": "application/json"},
                )
                return
            with self.state._lock:
                self.state.blobs[computed] = data
                self.state.uploads.pop(upload_id, None)
            self._send(201, b"", {"Content-Type": "application/octet-stream"})
            return

        self._send(405, b"", {})

    def _handle_manifest_put(self, repo: str, ref: str) -> None:
        if self._has_sid_cookie():
            self._send(
                403,
                b'{"errors":[{"code":"DENIED","message":"CSRF token invalid"}]}',
                {"Content-Type": "application/json"},
            )
            return
        body = self._read_body()
        try:
            manifest = json.loads(body)
        except ValueError:
            self._send(
                400,
                b'{"errors":[{"code":"MANIFEST_INVALID","message":"bad json"}]}',
                {"Content-Type": "application/json"},
            )
            return
        with self.state._lock:
            self.state.manifests.setdefault(repo, {})[ref] = manifest
            config = manifest.get("config") or {}
            config_digest = config.get("digest")
            if config_digest and config_digest not in self.state.blobs:
                self.state.blobs[config_digest] = b"{}"
        self._send(
            201,
            b"",
            {
                "Content-Type": MANIFEST_MEDIA_TYPE,
                "Docker-Content-Digest": sha256_digest(body),
            },
        )

    def _handle_v2_delete(self, m: re.Match[str]) -> None:
        if not self._has_basic():
            self._send(
                401,
                b'{"errors":[{"code":"UNAUTHORIZED","message":"auth required"}]}',
                {"Content-Type": "application/json"},
            )
            return
        if self.state.reject_artifact_delete:
            self._send(
                500,
                b'{"errors":[{"code":"INTERNAL","message":"delete rejected"}]}',
                {"Content-Type": "application/json"},
            )
            return
        # The v2.0 API path splits project/repo; manifests are keyed by the
        # full "project/repo" path used on the /v2/ Distribution API.
        full_repo = f"{m.group('project')}/{m.group('repo')}"
        ref = m.group("ref")
        with self.state._lock:
            repo_manifests = self.state.manifests.get(full_repo, {})
            removed = repo_manifests.pop(ref, None)
            # Mirror real Harbor: an empty repository disappears after its
            # last artifact is deleted.
            if removed is not None and not repo_manifests:
                self.state.manifests.pop(full_repo, None)
        if removed is None:
            self._send(
                404,
                b'{"errors":[{"code":"NOT_FOUND","message":"artifact not found"}]}',
                {"Content-Type": "application/json"},
            )
            return
        self._send(200, b"", {"Content-Type": "application/json"})

    def _handle_v2_repo_delete(self, m: re.Match[str]) -> None:
        """Repository-level DELETE (S-048 best-effort cleanup).

        404 when the repository is absent — real Harbor auto-removes an empty
        repository after its last artifact is deleted, so the robot account's
        explicit call normally finds it already gone.
        """
        if not self._has_basic():
            self._send(
                401,
                b'{"errors":[{"code":"UNAUTHORIZED","message":"auth required"}]}',
                {"Content-Type": "application/json"},
            )
            return
        full_repo = f"{m.group('project')}/{m.group('repo')}"
        with self.state._lock:
            manifests = self.state.manifests.get(full_repo)
            if not manifests:
                self._send(
                    404,
                    b'{"errors":[{"code":"NOT_FOUND","message":"repository not found"}]}',
                    {"Content-Type": "application/json"},
                )
                return
            self.state.manifests.pop(full_repo, None)
        self._send(200, b"", {"Content-Type": "application/json"})

    def _handle_token(self) -> None:
        if not self._has_basic():
            self._send(
                401,
                b'{"errors":[{"code":"UNAUTHORIZED","message":"auth required"}]}',
                {"Content-Type": "application/json"},
            )
            return
        self._send_json(
            200,
            {
                "token": _TOKEN,
                "access_token": _TOKEN,
                "expires_in": 300,
                "issued_at": "now",
            },
        )


class StubHarbor:
    """Threaded HTTP(S) stub exposing the test API used by fixtures."""

    def __init__(self) -> None:
        self.state = _StubHarborState()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.ca_cert_path: str = ""

    def start(self, tls: tuple[str, str] | None = None) -> str:
        """Bind on a random loopback port and serve in a background thread.

        ``tls`` = (certfile, keyfile) to serve HTTPS (both real clients use
        https against Harbor — oras-py defaults to https, HarborClient is
        pointed at https by the fixture). Trust the cert via
        ``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE`` in subprocess envs.

        Returns the base URL (``http(s)://127.0.0.1:{port}``).
        """
        import socket
        import ssl

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), StubHarborHandler)
        self._httpd.state = self.state  # type: ignore[attr-defined]
        if tls:
            certfile, keyfile = tls
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile, keyfile)
            self._httpd.socket = ctx.wrap_socket(self._httpd.socket, server_side=True)
            scheme = "https"
        else:
            scheme = "http"
        self.state.base_url = f"{scheme}://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        logger.info("Stub Harbor listening on %s", self.state.base_url)
        return self.state.base_url

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # -- test API ------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self.state.base_url

    def register_model(
        self, harbor_ref: str, files: dict[str, bytes]
    ) -> dict[str, Any]:
        return self.state.register_model(harbor_ref, files)

    def received_requests(self) -> list[tuple[str, str, dict[str, str]]]:
        return self.state.received_requests()

    def received_paths(self) -> list[str]:
        return [path for _, path, _ in self.state.received_requests()]

    def count_requests(self, method: str, path_contains: str) -> int:
        return sum(
            1
            for m, path, _ in self.state.received_requests()
            if m == method and path_contains in path
        )

    def reset(self) -> None:
        self.state.reset()

    # -- S-048 delete support ----------------------------------------------

    @property
    def reject_artifact_delete(self) -> bool:
        """When True, artifact deletes return 500 (simulates Harbor failure)."""
        return self.state.reject_artifact_delete

    @reject_artifact_delete.setter
    def reject_artifact_delete(self, value: bool) -> None:
        self.state.reject_artifact_delete = value

    def repo_manifest_count(self, repo: str) -> int:
        """Number of manifests still registered under *repo*."""
        return self.state.repo_manifest_count(repo)

    def get_manifest(self, repo: str, reference: str) -> dict[str, Any] | None:
        """Fetch a manifest by repository and reference (None when deleted)."""
        return self.state.get_manifest(repo, reference)
