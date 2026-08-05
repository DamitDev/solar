"""repo_path: catalog delete relay (marker: repo_path).

Proves the S-048 delete flow end-to-end: Solar Control deletes the Harbor
artifact first, then unregisters in the Data Repository. Registry rows and
Harbor manifests must both disappear; a Harbor failure must leave the
metadata intact so the operator can retry.
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.constants import FIXTURE_MODEL_DIR, harbor_port
from fixtures.seed import read_test_model_files, register_model_in_data_repo

pytestmark = pytest.mark.repo_path


async def _unique_name(prefix: str = "del-model") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _register(stack, http_data_repo, name: str, version: str) -> None:
    """Push a model into (stub) Harbor and register it in the Data Repository."""
    harbor_ref = f"127.0.0.1:{harbor_port(stack.harbor_ref)}/supernova/{name}:{version}"
    stack.stub_harbor.register_model(
        harbor_ref, read_test_model_files(FIXTURE_MODEL_DIR)
    )
    await register_model_in_data_repo(
        http_data_repo, name=name, harbor_ref=harbor_ref, version=version
    )


async def test_delete_version_removes_registry_and_harbor(
    http_data_repo, http_control, stack, clean_state
):
    """DELETE /api/catalog/models/{name}/versions/{v} -> 204, both sides gone."""
    name = await _unique_name()
    await _register(stack, http_data_repo, name, "v1")
    await _register(stack, http_data_repo, name, "v2")
    stack.stub_harbor.reset()

    resp = await http_control.delete(f"/api/catalog/models/{name}/versions/v1")
    assert resp.status_code == 204, resp.text

    # Data Repository: v1 unregistered, v2 remains.
    listing = await http_data_repo.get(f"/api/models/{name}/versions")
    assert listing.status_code == 200
    assert [v["version"] for v in listing.json()["versions"]] == ["v2"]

    # Harbor: v1 manifest gone, v2 still present.
    assert stack.stub_harbor.repo_manifest_count(f"supernova/{name}") == 1
    assert stack.stub_harbor.get_manifest(f"supernova/{name}", "v2") is not None
    assert stack.stub_harbor.get_manifest(f"supernova/{name}", "v1") is None

    # The catalog proxy no longer exposes v1.
    resp = await http_control.get(f"/api/catalog/models/{name}/versions")
    assert resp.status_code == 200
    assert [v["version"] for v in resp.json()["versions"]] == ["v2"]


async def test_delete_repository_removes_everything(
    http_data_repo, http_control, stack, clean_state
):
    """DELETE /api/catalog/models/{name} -> 200 with results, all sides gone."""
    name = await _unique_name()
    await _register(stack, http_data_repo, name, "v1")
    await _register(stack, http_data_repo, name, "v2")
    stack.stub_harbor.reset()

    resp = await http_control.delete(f"/api/catalog/models/{name}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == name
    assert body["deleted"] == ["v2", "v1"]
    assert body["failed"] == []
    assert body["artifact_removed"] is True
    # The repository was auto-removed with its last artifact -> 404 -> gone.
    assert body["harbor_repository_removed"] is True

    # Registry row gone — no ghost entry with versions_count: 0.
    resp = await http_data_repo.get(f"/api/models/{name}")
    assert resp.status_code == 404

    # Harbor: no manifests left under the repository.
    assert stack.stub_harbor.repo_manifest_count(f"supernova/{name}") == 0

    # The catalog list no longer contains the model.
    listing = await http_control.get("/api/catalog/models")
    assert listing.status_code == 200
    assert name not in [item["name"] for item in listing.json()["items"]]


async def test_delete_rejected_by_harbor_keeps_metadata(
    http_data_repo, http_control, stack, clean_state
):
    """A Harbor delete failure -> 502 and the metadata survives for a retry."""
    name = await _unique_name()
    await _register(stack, http_data_repo, name, "v1")
    stack.stub_harbor.reset()
    stack.stub_harbor.reject_artifact_delete = True

    resp = await http_control.delete(f"/api/catalog/models/{name}/versions/v1")
    assert resp.status_code == 502, resp.text

    # Registry untouched.
    listing = await http_data_repo.get(f"/api/models/{name}/versions")
    assert listing.status_code == 200
    assert [v["version"] for v in listing.json()["versions"]] == ["v1"]

    # Harbor manifest untouched.
    assert stack.stub_harbor.get_manifest(f"supernova/{name}", "v1") is not None

    # The catalog still lists the version.
    resp = await http_control.get(f"/api/catalog/models/{name}/versions")
    assert resp.status_code == 200
    assert [v["version"] for v in resp.json()["versions"]] == ["v1"]
