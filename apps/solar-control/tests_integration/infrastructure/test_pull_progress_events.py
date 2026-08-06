"""infrastructure: C4 — model pull progress is cached and observable
(marker: infrastructure).

Hosts emit pull_progress over the WS channel; control caches the latest
per (host, source_uri) in Redis and exposes it via GET /api/pulls.

- SUCCESS: a cold-start intent (model caches wiped) ends with a terminal
  `completed` entry for its model source, and the intent reaches ready.
- FAILURE: an artifact that resolves in data-repo but 404s at pull time
  ends with a terminal `failed` entry.
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.constants import MODEL_NAME, MODEL_SOURCE_URI
from fixtures.helpers import wait_for
from fixtures.intents import create_intent, wait_intent_ready
from fixtures.seed import register_model_in_data_repo

pytestmark = pytest.mark.infrastructure


def _alias(prefix: str = "pull") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _pulls(http_control) -> dict:
    resp = await http_control.get("/api/pulls")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _entry_for(pulls: dict, source_uri: str) -> dict | None:
    for field, entry in pulls.items():
        if field.endswith(f"|{source_uri}"):
            return entry
    return None


async def test_cold_start_pull_ends_with_completed_entry(
    http_control, stack, clean_state
):
    """The fixture model is not cached after clean_state, so the intent's
    CREATE pulls it; the pull leaves a terminal completed entry."""
    intent = await create_intent(http_control, alias=_alias())

    async def terminal_entry() -> bool:
        entry = _entry_for(await _pulls(http_control), MODEL_SOURCE_URI)
        return entry is not None and entry.get("data", {}).get("phase") in (
            "completed",
            "failed",
        )

    await wait_for(
        terminal_entry,
        timeout=60.0,
        interval=0.5,
        description="terminal pull-progress entry for the fixture model",
    )
    entry = _entry_for(await _pulls(http_control), MODEL_SOURCE_URI)
    assert entry is not None
    data = entry["data"]
    assert data["source_uri"] == MODEL_SOURCE_URI
    assert data["phase"] == "completed"
    # The entry carries the at timestamp and the payload shape.
    assert entry.get("at")
    assert "bytes_done" in data

    # The pull actually worked: the intent reaches ready.
    ready = await wait_intent_ready(http_control, intent["id"])
    assert ready["status"]["ready_replicas"] == 1


async def test_failed_pull_ends_with_failed_entry(
    http_control, http_data_repo, stack, clean_state
):
    """An artifact that resolves in data-repo but is gone from Harbor at
    pull time leaves a terminal failed entry."""
    ghost_name = f"ghost-{uuid.uuid4().hex[:6]}"
    ghost_version = "v1"
    host_port = stack.harbor_ref.split("/")[0].split(":")[-1]
    ghost_ref = f"127.0.0.1:{host_port}/supernova/{ghost_name}:{ghost_version}"
    source_uri = f"repo://{ghost_name}:{ghost_version}"

    # Register the artifact in the stub AND data-repo (registration
    # verifies Harbor), then remove the manifest from the stub so the
    # host-side pull 404s.
    files = {
        "model.safetensors": b"\x00" * 64,
        "config.json": b"{}",
        "tokenizer.json": b"{}",
    }
    stack.stub_harbor.register_model(ghost_ref, files)
    await register_model_in_data_repo(
        http_data_repo,
        name=ghost_name,
        harbor_ref=ghost_ref,
        version=ghost_version,
    )
    from fixtures.stub_harbor import split_ref

    repo, ref = split_ref(ghost_ref)  # host stripped: ("supernova/ghost-xxx", "v1")
    assert ref == ghost_version
    manifests = stack.stub_harbor.state.manifests
    original_manifest = manifests[repo].pop(ghost_version)
    try:
        intent = await create_intent(
            http_control, alias=_alias(), model_source=source_uri
        )

        async def failed_entry() -> bool:
            entry = _entry_for(await _pulls(http_control), source_uri)
            return entry is not None and entry.get("data", {}).get("phase") == "failed"

        await wait_for(
            failed_entry,
            timeout=60.0,
            interval=0.5,
            description="failed pull-progress entry for the ghost model",
        )
        entry = _entry_for(await _pulls(http_control), source_uri)
        assert entry is not None
        assert entry["data"]["phase"] == "failed"
        assert entry["data"]["source_uri"] == source_uri

        # The intent does not fake readiness for an unpullable model.
        current = await http_control.get(f"/api/intents/{intent['id']}")
        assert current.status_code == 200
        assert current.json()["status"]["ready_replicas"] == 0
    finally:
        # Restore the manifest so other tests are unaffected.
        manifests[repo][ghost_version] = original_manifest
