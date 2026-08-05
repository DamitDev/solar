"""Artifact upload session state in Redis (S-047).

Sessions live in Redis so either Solar Control replica can serve any
request of a session. The declared file list is stored once in the
``data`` hash field; each completed file records its digest in a separate
``file:{path}`` hash field. That split means concurrent file uploads (the
WebUI sends 2-3 files in parallel, potentially to different replicas) never
read-modify-write the same field — a digest can only be lost by expiring
the whole session, not by a lost update.

The ``state`` hash field is authoritative; the ``state`` key inside the
``data`` document exists for humans debugging Redis and is overwritten on
every read.

State machine: ``pending`` -> ``uploading`` -> ``completing`` ->
``completed`` | ``failed`` | ``aborted``.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.config import settings
from app.redis_state.connection import redis_client

KEY_PREFIX = "solar:uploads:"
STATE_FIELD = "state"
DATA_FIELD = "data"
FILE_PREFIX = "file:"

UPLOAD_STATES = ("pending", "uploading", "completing", "completed", "failed", "aborted")


def _key(upload_id: str) -> str:
    return f"{KEY_PREFIX}{upload_id}"


def new_upload_id() -> str:
    return uuid.uuid4().hex


class UploadSessionStore:
    """Redis hash CRUD for upload sessions with per-file digest recording."""

    async def create(
        self,
        *,
        category: str,
        name: str,
        version: str,
        repo: str,
        harbor_ref: str,
        metadata: dict[str, Any],
        files: list[dict[str, Any]],
    ) -> str:
        """Persist a new session and return its upload_id."""
        upload_id = new_upload_id()
        data = {
            "upload_id": upload_id,
            "category": category,
            "name": name,
            "version": version,
            "repo": repo,
            "harbor_ref": harbor_ref,
            "metadata": metadata,
            "files": files,
            "state": "pending",
            "created_at": int(time.time()),
        }
        r = redis_client()
        pipe = r.pipeline()
        pipe.hset(_key(upload_id), DATA_FIELD, json.dumps(data))
        pipe.hset(_key(upload_id), STATE_FIELD, "pending")
        pipe.expire(_key(upload_id), settings.upload_session_ttl_s)
        await pipe.execute()
        return upload_id

    async def get(self, upload_id: str) -> dict[str, Any] | None:
        """Return the session document with the authoritative state applied."""
        r = redis_client()
        raw = await r.hget(_key(upload_id), DATA_FIELD)
        if raw is None:
            return None
        data = json.loads(raw)
        state = await r.hget(_key(upload_id), STATE_FIELD)
        if state:
            data["state"] = state
        return data

    async def set_state(self, upload_id: str, state: str) -> None:
        """Atomically update the authoritative state field."""
        if state not in UPLOAD_STATES:
            raise ValueError(f"Unknown upload state {state!r}")
        r = redis_client()
        await r.hset(_key(upload_id), STATE_FIELD, state)

    async def record_file(
        self,
        upload_id: str,
        path: str,
        *,
        digest: str,
        size: int,
    ) -> None:
        """Record a completed file digest and refresh the session TTL."""
        r = redis_client()
        pipe = r.pipeline()
        pipe.hset(
            _key(upload_id),
            f"{FILE_PREFIX}{path}",
            json.dumps({"digest": digest, "size": size}),
        )
        pipe.expire(_key(upload_id), settings.upload_session_ttl_s)
        await pipe.execute()

    async def get_file(self, upload_id: str, path: str) -> dict[str, Any] | None:
        r = redis_client()
        raw = await r.hget(_key(upload_id), f"{FILE_PREFIX}{path}")
        if raw is None:
            return None
        return json.loads(raw)

    async def list_files(self, upload_id: str) -> dict[str, dict[str, Any]]:
        """Return ``{path: {digest, size}}`` for every uploaded file."""
        r = redis_client()
        raw = await r.hgetall(_key(upload_id))
        files: dict[str, dict[str, Any]] = {}
        for field, value in raw.items():
            if field.startswith(FILE_PREFIX):
                path = field[len(FILE_PREFIX) :]
                files[path] = json.loads(value)
        return files

    async def touch(self, upload_id: str) -> None:
        """Refresh the session TTL (called on each file completion)."""
        r = redis_client()
        await r.expire(_key(upload_id), settings.upload_session_ttl_s)

    async def delete(self, upload_id: str) -> None:
        r = redis_client()
        await r.delete(_key(upload_id))
