"""Tests for app.services.catalog_delete — CatalogDeleteService (S-048).

The OCI client and the Data Repository client are fakes; the running-instance
guard patches the shared collector. Tests assert the Harbor-first ordering,
404 tolerance on every step, the ``latest`` alias guard, and per-version
partial-failure semantics.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest
from fastapi import HTTPException

from app.harbor.oci_push import OciPushError
from app.services.catalog_delete import (
    CatalogDeleteService,
    RunningInstance,
    _blocking_instances,
    _instance_serves_version,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_running_instances():
    """Unit tests never touch the instance cache or DB.

    The guard is patched to report no running instances by default; tests
    that exercise the guard override the patch with their own return value.
    """
    with patch(
        "app.services.catalog_delete.collect_running_instances",
        return_value=[],
    ):
        yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _version(version: str) -> dict:
    return {
        "version": version,
        "harbor_ref": f"imgrepo.damit.hu/supernova/mymodel:{version}",
        "created_at": "2026-08-05T00:00:00Z",
        "size_bytes": 1024,
        "checksum": "sha256:abc",
    }


def _versions_body(*versions: str) -> dict:
    return {"versions": [_version(v) for v in versions]}


class FakeDataRepo:
    """Scriptable Data Repository client recording every call."""

    def __init__(
        self,
        *,
        versions_status: int = 200,
        versions_body: dict | None = None,
        delete_status: int = 204,
        delete_body: dict | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.versions_status = versions_status
        self.versions_body = versions_body or {"versions": []}
        self.delete_status = delete_status
        self.delete_body = delete_body or {}

    async def get(self, path: str):
        self.calls.append(("GET", path))
        if path.endswith("/versions"):
            return self.versions_status, self.versions_body
        return 404, {}

    async def delete(self, path: str):
        self.calls.append(("DELETE", path))
        return self.delete_status, self.delete_body

    async def post(self, path: str, json):
        raise AssertionError("delete service must never POST")


class FakeOci:
    """Scriptable OCI client; call args are asserted via await_args_list."""

    def __init__(self) -> None:
        self.delete_tag = AsyncMock(return_value=None)
        self.delete_repository = AsyncMock(return_value=True)


def _make_service(
    *,
    data_repo: FakeDataRepo | None = None,
    oci: FakeOci | None = None,
) -> tuple[CatalogDeleteService, FakeDataRepo, FakeOci]:
    dr = data_repo or FakeDataRepo()
    oc = oci or FakeOci()
    return CatalogDeleteService(oci=oc, data_repo=dr), dr, oc


def _instance(
    *,
    name: str = "mymodel",
    version: str | None = "v1",
    instance_id: str = "i1",
    host_name: str = "host-1",
    source: str | None = None,
) -> RunningInstance:
    return RunningInstance(
        host_id="h1",
        host_name=host_name,
        instance_id=instance_id,
        source=source or f"repo://{name}:{version}",
        name=name,
        version=version,
    )


# ---------------------------------------------------------------------------
# Guard units
# ---------------------------------------------------------------------------


def test_instance_serves_version_exact_match():
    inst = _instance(name="mymodel", version="v2")
    assert _instance_serves_version(inst, "mymodel", "v2", "v2") is True


def test_instance_serves_version_other_version_does_not_block():
    inst = _instance(name="mymodel", version="v2")
    assert _instance_serves_version(inst, "mymodel", "v1", "v2") is False


def test_instance_serves_version_other_model_does_not_block():
    inst = _instance(name="other", version="v2")
    assert _instance_serves_version(inst, "mymodel", "v2", "v2") is False


def test_instance_serves_latest_blocks_newest_version():
    inst = _instance(name="mymodel", version="latest", source="repo://mymodel:latest")
    assert _instance_serves_version(inst, "mymodel", "v2", "v2") is True
    assert _instance_serves_version(inst, "mymodel", "v1", "v2") is False


def test_instance_serves_any_version_for_artifact_delete():
    inst = _instance(name="mymodel", version="v1")
    assert _instance_serves_version(inst, "mymodel", None, "v2") is True


def test_huggingface_instance_blocks_artifact_delete_only():
    hf = RunningInstance(
        host_id="h1",
        host_name="host-1",
        instance_id="i9",
        source="huggingface://org/mymodel",
        name="org/mymodel",
        version=None,
    )
    # Same catalog name via huggingface scheme -> artifact delete blocked.
    assert _instance_serves_version(hf, "org/mymodel", None, None) is True
    # Version delete cannot match a non-repo source.
    assert _instance_serves_version(hf, "org/mymodel", "v1", "v1") is False


def test_blocking_instances_filters():
    instances = [
        _instance(instance_id="a", version="v1"),
        _instance(instance_id="b", version="v2"),
        _instance(instance_id="c", name="other", version="v1"),
    ]
    blockers = _blocking_instances(instances, "mymodel", "v1", "v2")
    assert [b.instance_id for b in blockers] == ["a"]


# ---------------------------------------------------------------------------
# delete_version
# ---------------------------------------------------------------------------


async def test_delete_version_harbor_first_then_unregister():
    svc, dr, oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v2", "v1"))
    )

    await svc.delete_version("mymodel", "v1")

    assert oc.delete_tag.await_args_list == [call("supernova/mymodel", "v1")]
    assert dr.calls == [
        ("GET", "/api/models/mymodel/versions"),
        ("DELETE", "/api/models/mymodel/versions/v1"),
    ]
    assert len(dr.calls) == 2


async def test_delete_version_uses_newest_for_latest_guard():
    svc, _dr, oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v2", "v1"))
    )
    with (
        patch(
            "app.services.catalog_delete.collect_running_instances",
            return_value=[_instance(version="latest", source="repo://mymodel:latest")],
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await svc.delete_version("mymodel", "v2")
    assert exc.value.status_code == 409
    oc.delete_tag.assert_not_awaited()  # guard runs before any Harbor call


async def test_delete_version_allowed_when_latest_but_not_newest():
    svc, _dr, oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v2", "v1"))
    )
    with patch(
        "app.services.catalog_delete.collect_running_instances",
        return_value=[_instance(version="latest", source="repo://mymodel:latest")],
    ):
        await svc.delete_version("mymodel", "v1")

    assert oc.delete_tag.await_args_list == [call("supernova/mymodel", "v1")]


async def test_delete_version_blocked_by_running_instance():
    svc, dr, oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v1"))
    )
    with (
        patch(
            "app.services.catalog_delete.collect_running_instances",
            return_value=[_instance(version="v1")],
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await svc.delete_version("mymodel", "v1")

    assert exc.value.status_code == 409
    assert "i1@host-1" in exc.value.detail
    assert oc.delete_tag.await_count == 0
    assert dr.calls == [("GET", "/api/models/mymodel/versions")]


async def test_delete_version_unregister_404_tolerated():
    svc, _dr, oc = _make_service(
        data_repo=FakeDataRepo(
            versions_body=_versions_body("v1"),
            delete_status=404,
        ),
    )

    await svc.delete_version("mymodel", "v1")  # must not raise

    assert oc.delete_tag.await_args_list == [call("supernova/mymodel", "v1")]


async def test_delete_version_unregister_500_becomes_502():
    svc, _dr, _oc = _make_service(
        data_repo=FakeDataRepo(
            versions_body=_versions_body("v1"),
            delete_status=500,
            delete_body={"detail": "boom"},
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await svc.delete_version("mymodel", "v1")
    assert exc.value.status_code == 502
    assert "boom" in exc.value.detail


async def test_delete_version_harbor_error_becomes_502_and_skips_unregister():
    svc, dr, oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v1"))
    )
    oc.delete_tag.side_effect = OciPushError("forbidden", status_code=403)

    with pytest.raises(HTTPException) as exc:
        await svc.delete_version("mymodel", "v1")

    assert exc.value.status_code == 502
    assert dr.calls == [("GET", "/api/models/mymodel/versions")]


async def test_delete_version_rejects_latest_alias():
    svc, dr, _oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v1"))
    )
    with pytest.raises(HTTPException) as exc:
        await svc.delete_version("mymodel", "latest")
    assert exc.value.status_code == 422
    assert dr.calls == []


async def test_delete_version_invalid_name_422():
    svc, dr, _oc = _make_service()
    with pytest.raises(HTTPException) as exc:
        await svc.delete_version("BAD NAME", "v1")
    assert exc.value.status_code == 422
    assert dr.calls == []


async def test_delete_version_unknown_model_404():
    svc, _dr, _oc = _make_service(data_repo=FakeDataRepo(versions_status=404))
    with pytest.raises(HTTPException) as exc:
        await svc.delete_version("ghost", "v1")
    assert exc.value.status_code == 404


async def test_delete_version_unknown_version_404():
    svc, _dr, oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v1"))
    )
    with pytest.raises(HTTPException) as exc:
        await svc.delete_version("mymodel", "v99")
    assert exc.value.status_code == 404
    assert oc.delete_tag.await_count == 0


async def test_delete_version_upstream_500_becomes_502():
    svc, _dr, _oc = _make_service(
        data_repo=FakeDataRepo(
            versions_status=500, versions_body={"detail": "db down"}
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await svc.delete_version("mymodel", "v1")
    assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# delete_artifact
# ---------------------------------------------------------------------------


async def test_delete_artifact_all_clean_removes_row_and_repo():
    svc, dr, oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v2", "v1"))
    )

    result = await svc.delete_artifact("mymodel")

    assert oc.delete_tag.await_args_list == [
        call("supernova/mymodel", "v2"),
        call("supernova/mymodel", "v1"),
    ]
    assert dr.calls == [
        ("GET", "/api/models/mymodel/versions"),
        ("DELETE", "/api/models/mymodel"),
    ]
    assert result.deleted == ["v2", "v1"]
    assert result.failed == []
    assert result.artifact_removed is True
    assert result.harbor_repository_removed is True
    oc.delete_repository.assert_awaited_once_with("supernova/mymodel")


async def test_delete_artifact_unregister_404_tolerated():
    svc, _dr, _oc = _make_service(
        data_repo=FakeDataRepo(
            versions_body=_versions_body("v1"),
            delete_status=404,
        ),
    )

    result = await svc.delete_artifact("mymodel")

    assert result.artifact_removed is True
    assert result.harbor_repository_removed is True


async def test_delete_artifact_partial_failure_keeps_artifact_row():
    svc, dr, oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v2", "v1"))
    )

    async def fail_second(repo: str, reference: str) -> None:
        if reference == "v1":
            raise OciPushError("boom", status_code=500)

    oc.delete_tag.side_effect = fail_second

    result = await svc.delete_artifact("mymodel")

    assert oc.delete_tag.await_args_list == [
        call("supernova/mymodel", "v2"),
        call("supernova/mymodel", "v1"),
    ]
    # The clean version is unregistered individually; the artifact row stays.
    assert dr.calls == [
        ("GET", "/api/models/mymodel/versions"),
        ("DELETE", "/api/models/mymodel/versions/v2"),
    ]
    assert result.deleted == ["v2"]
    assert [f.version for f in result.failed] == ["v1"]
    assert result.artifact_removed is False
    assert result.harbor_repository_removed is False
    oc.delete_repository.assert_not_awaited()


async def test_delete_artifact_partial_unregister_failure_reported():
    svc, _dr, _oc = _make_service(
        data_repo=FakeDataRepo(
            versions_body=_versions_body("v1"),
            delete_status=500,
            delete_body={"detail": "boom"},
        ),
    )

    result = await svc.delete_artifact("mymodel")

    # The version was deleted from Harbor, but the artifact row could not be
    # removed — reported as failed so the operator retries.
    assert result.deleted == ["v1"]
    assert result.failed[0].version == "*"
    assert result.artifact_removed is False


async def test_delete_artifact_no_versions_removes_ghost_row():
    svc, dr, oc = _make_service(data_repo=FakeDataRepo(versions_body={"versions": []}))

    result = await svc.delete_artifact("mymodel")

    oc.delete_tag.assert_not_awaited()
    assert dr.calls == [
        ("GET", "/api/models/mymodel/versions"),
        ("DELETE", "/api/models/mymodel"),
    ]
    assert result.deleted == []
    assert result.artifact_removed is True
    oc.delete_repository.assert_awaited_once_with("supernova/mymodel")


async def test_delete_artifact_blocked_by_any_running_instance():
    svc, dr, oc = _make_service(
        data_repo=FakeDataRepo(versions_body=_versions_body("v1", "v0"))
    )
    with (
        patch(
            "app.services.catalog_delete.collect_running_instances",
            return_value=[_instance(version="v0")],
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await svc.delete_artifact("mymodel")

    assert exc.value.status_code == 409
    assert oc.delete_tag.await_count == 0
    assert dr.calls == [("GET", "/api/models/mymodel/versions")]


async def test_delete_artifact_unknown_model_404():
    svc, _dr, _oc = _make_service(data_repo=FakeDataRepo(versions_status=404))
    with pytest.raises(HTTPException) as exc:
        await svc.delete_artifact("ghost")
    assert exc.value.status_code == 404


async def test_delete_artifact_invalid_name_422():
    svc, dr, _oc = _make_service()
    with pytest.raises(HTTPException) as exc:
        await svc.delete_artifact("BAD NAME")
    assert exc.value.status_code == 422
    assert dr.calls == []
