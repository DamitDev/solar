"""repo_path: artifact upload relay end-to-end (S-047, marker: repo_path).

Exercises the full interactive upload path against the stack: session
creation with pre-flight checks, streaming file uploads through the relay
into the (stub) Harbor write path, manifest push + Data Repository
registration, rollback on registration failure, and the closing loop —
Solar Host pulling the uploaded artifact and verifying its digests
(S-045/S-046 layout contract).
"""

from __future__ import annotations

import hashlib

import pytest

pytestmark = pytest.mark.repo_path

MIIB = 1024 * 1024


def _model_files() -> dict[str, bytes]:
    """A small but realistic flat model artifact (spec §2.1 layout)."""
    return {
        "config.json": b'{"model_type": "test", "quant": "Q4_K_M"}',
        "model-Q4_K_M.gguf": b"G" * (2 * MIIB),
        "tokenizer.json": b"{}",
    }


def _dataset_files() -> dict[str, bytes]:
    return {
        "train.parquet": b"P" * (1 * MIIB),
        "metadata.json": b'{"rows": 100}',
    }


async def _create_session(
    http_control,
    name: str,
    files: dict[str, bytes],
    *,
    category: str = "model",
    version: str | None = None,
    metadata: dict | None = None,
) -> dict:
    payload = {
        "category": category,
        "name": name,
        "files": [{"path": path, "size": len(data)} for path, data in files.items()],
    }
    if version is not None:
        payload["version"] = version
    if metadata is not None:
        payload["metadata"] = metadata
    resp = await http_control.post("/api/uploads", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _upload_files(http_control, upload_id: str, files: dict[str, bytes]) -> None:
    for path, data in files.items():
        resp = await http_control.put(
            f"/api/uploads/{upload_id}/files",
            params={"path": path},
            content=data,
        )
        assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text}"
        result = resp.json()
        assert result["path"] == path
        assert result["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
        assert result["size"] == len(data)


async def _complete(http_control, upload_id: str) -> dict:
    resp = await http_control.post(f"/api/uploads/{upload_id}/complete")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_upload_multi_file_artifact_end_to_end(http_control, stack, clean_state):
    files = _model_files()
    session = await _create_session(http_control, "test-upload-e2e", files)
    await _upload_files(http_control, session["upload_id"], files)
    result = await _complete(http_control, session["upload_id"])

    assert result["name"] == "test-upload-e2e"
    assert result["version"] == "v1"
    assert result["category"] == "model"
    assert result["size_bytes"] == sum(len(b) for b in files.values())
    assert result["harbor_ref"].endswith("/supernova/test-upload-e2e:v1")

    # The relay chunked each file into Harbor, plus the config blob on
    # complete: open + PATCH + close per blob (3 files + 1 config).
    assert stack.stub_harbor.count_requests("PATCH", "/blobs/uploads/") == 4
    assert stack.stub_harbor.count_requests("PUT", "/manifests/v1") >= 1

    # The version is visible through the catalog (data-repo + enrichment).
    resp = await http_control.get("/api/catalog/models", params={"search": "e2e"})
    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["items"]]
    assert "test-upload-e2e" in names


async def test_uploaded_artifact_is_pullable_by_host(http_control, stack, clean_state):
    """Closes the loop with S-045/S-046: host pulls + verifies the upload."""
    files = _model_files()
    session = await _create_session(http_control, "test-upload-host-pull", files)
    await _upload_files(http_control, session["upload_id"], files)
    await _complete(http_control, session["upload_id"])

    hosts = (await http_control.get("/api/hosts")).json()
    host = next(h for h in hosts if h["name"] == "host-a")
    resp = await http_control.post(
        "/api/models/distribute",
        json={
            "target_host_id": host["id"],
            "source_uri": "repo://test-upload-host-pull:v1",
        },
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert results[0]["cached"] is False

    # Solar Host pulled the flat artifact and its digest verification passed
    # (a failure would have surfaced as 502 with a ModelPullError detail).
    slug_dir = stack.models_dir_a / "repo--test-upload-host-pull--v1"
    pulled = {p.name: p.read_bytes() for p in slug_dir.iterdir() if p.is_file()}
    assert pulled == files, "pulled files differ from the uploaded bytes"


async def test_upload_nested_paths_round_trip(http_control, stack, clean_state):
    """Nested layout survives upload -> pull -> verify (S-046 regression)."""
    files = {
        "config.json": b"{}",
        "weights/shard-1.bin": b"W1" * 1024,
        "weights/shard-2.bin": b"W2" * 1024,
    }
    session = await _create_session(http_control, "test-upload-nested", files)
    await _upload_files(http_control, session["upload_id"], files)
    await _complete(http_control, session["upload_id"])

    hosts = (await http_control.get("/api/hosts")).json()
    host = next(h for h in hosts if h["name"] == "host-b")
    resp = await http_control.post(
        "/api/models/distribute",
        json={
            "target_host_id": host["id"],
            "source_uri": "repo://test-upload-nested:v1",
        },
    )
    assert resp.status_code == 200, resp.text

    slug_dir = stack.models_dir_b / "repo--test-upload-nested--v1"
    assert (slug_dir / "weights" / "shard-1.bin").read_bytes() == files[
        "weights/shard-1.bin"
    ]
    assert (slug_dir / "weights" / "shard-2.bin").read_bytes() == files[
        "weights/shard-2.bin"
    ]
    assert (slug_dir / "config.json").read_bytes() == files["config.json"]


async def test_upload_conflicting_version_rejected(http_control, stack, clean_state):
    files = _model_files()
    session = await _create_session(
        http_control, "test-upload-conflict", files, version="v1"
    )
    await _upload_files(http_control, session["upload_id"], files)
    await _complete(http_control, session["upload_id"])

    stack.stub_harbor.reset()
    resp = await http_control.post(
        "/api/uploads",
        json={
            "category": "model",
            "name": "test-upload-conflict",
            "version": "v1",
            "files": [{"path": "config.json", "size": 2}],
        },
    )
    assert resp.status_code == 409, resp.text
    # The pre-flight conflict must fail before any byte reaches Harbor.
    assert stack.stub_harbor.count_requests("PATCH", "/blobs/uploads/") == 0


async def test_upload_registration_failure_rolls_back_tag(
    http_control, http_data_repo, stack, clean_state
):
    """Registration failure deletes the pushed Harbor tag (spec §4.5)."""
    # A dataset with an unsupported format passes Solar Control's checks but
    # is rejected by the Data Repository on registration (422) — after the
    # manifest was already pushed.
    files = _dataset_files()
    session = await _create_session(
        http_control,
        "test-upload-rollback",
        files,
        category="dataset",
        version="v1",
        metadata={"format": "bogus"},
    )
    await _upload_files(http_control, session["upload_id"], files)

    resp = await http_control.post(f"/api/uploads/{session['upload_id']}/complete")
    assert resp.status_code == 422, resp.text
    assert "metadata.format" in str(resp.json()["detail"])

    # Rollback: the stub Harbor recorded the artifact delete, and the
    # manifest is gone so a retry is not blocked.
    deletes = [
        path
        for method, path, _headers in stack.stub_harbor.received_requests()
        if method == "DELETE"
    ]
    assert any(
        "test-upload-rollback" in path and path.endswith("/artifacts/v1")
        for path in deletes
    ), deletes
    assert (
        stack.stub_harbor.state.get_manifest("supernova/test-upload-rollback", "v1")
        is None
    )


async def test_upload_large_file_multi_chunk(http_control, stack, clean_state):
    """A ~24 MiB file streams as three 8 MiB PATCHes (spec §4.4 #3)."""
    files = {"big.bin": b"B" * (24 * MIIB)}
    session = await _create_session(http_control, "test-upload-big", files)
    await _upload_files(http_control, session["upload_id"], files)
    await _complete(http_control, session["upload_id"])

    # One open, three PATCHes, one close for the single file; the config
    # blob on complete adds a second one-chunk blob upload.
    assert stack.stub_harbor.count_requests("PATCH", "/blobs/uploads/") == 4
