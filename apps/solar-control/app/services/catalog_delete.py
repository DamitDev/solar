"""Catalog artifact deletion orchestration (S-048).

Mirrors the upload relay direction (S-047): Solar Control deletes the Harbor
artifact first, then unregisters the metadata in the Data Repository.

Ordering is deliberate — Harbor first, unregister second. If the unregister
fails after Harbor succeeded, a retry still converges because the Harbor tag
delete tolerates a missing tag (404); the reverse order would orphan a blob
with no reference left to find it by.

Every step of the flow tolerates "already gone" (404): the Harbor tag delete,
the per-version unregister, and the artifact-row unregister.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import BaseModel

from app.database.hosts import host_db
from app.harbor.oci_push import OciPushClient, OciPushError, parse_repo
from app.model_resolvers.parser import HuggingFaceURI, RepoURI, parse
from app.services.uploads import HARBOR_PROJECT, DataRepoClient

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")


class VersionDeleteFailure(BaseModel):
    """One version whose Harbor delete or unregister did not succeed."""

    version: str
    detail: str


class DeleteArtifactResult(BaseModel):
    """Per-version outcome of a whole-repository delete."""

    name: str
    deleted: list[str]
    failed: list[VersionDeleteFailure]
    artifact_removed: bool
    harbor_repository_removed: bool


@dataclass(frozen=True)
class RunningInstance:
    """A running instance and the model source it serves (guard evidence)."""

    host_id: str
    host_name: str
    instance_id: str
    source: str
    name: str | None
    version: str | None


def _model_name_from_source(source: str) -> str | None:
    """Extract the catalog model name from a model source URI.

    ``repo://name:version/...`` -> ``name``; ``huggingface://org/model``
    -> ``org/model``; anything unparsable -> None.
    """
    try:
        parsed = parse(source)
    except HTTPException:
        return None
    if isinstance(parsed, RepoURI):
        return parsed.name
    if isinstance(parsed, HuggingFaceURI):
        return parsed.model_id
    return None


def _source_version(source: str) -> str | None:
    """Extract the version from a ``repo://name:version`` source, if parseable."""
    version = None
    try:
        parsed = parse(source)
    except HTTPException:
        return None
    if isinstance(parsed, RepoURI):
        version = parsed.version
    return version


async def collect_running_instances() -> list[RunningInstance]:
    """Scan every host's cached instance state for running instances.

    Shared with the catalog route enrichment (D-018): the catalog groups by
    model name, the delete service additionally matches versions.
    """
    from app.socketio_app.host_handlers import get_host_instances

    hosts = await host_db.get_all_hosts()
    matches: list[RunningInstance] = []
    for host in hosts:
        instances = await get_host_instances(host.id)
        for inst in instances:
            if inst.get("status") != "running":
                continue
            config = inst.get("config") or {}
            source = config.get("model_source") or inst.get("model_source")
            if not source:
                continue
            matches.append(
                RunningInstance(
                    host_id=host.id,
                    host_name=host.name,
                    instance_id=str(inst.get("id", "")),
                    source=source,
                    name=_model_name_from_source(source),
                    version=_source_version(source),
                )
            )
    return matches


def _instance_serves_version(
    instance: RunningInstance,
    name: str,
    version: str | None,
    newest_version: str | None,
) -> bool:
    """True when *instance* serves *name* — and, for version deletes, *version*.

    For a version delete, ``repo://name:latest`` resolves to the artifact's
    newest version, so deleting the newest version is blocked while such an
    instance runs — otherwise ``latest`` would silently re-point at an older
    version. An artifact delete is blocked by any instance serving the model,
    regardless of source scheme.
    """
    if instance.name != name:
        return False
    if version is None:
        return True
    if instance.version is None:
        return False
    if instance.version == version:
        return True
    return (
        instance.version.lower() == "latest"
        and newest_version is not None
        and version == newest_version
    )


def _blocking_instances(
    instances: list[RunningInstance],
    name: str,
    version: str | None,
    newest_version: str | None,
) -> list[RunningInstance]:
    """Filter *instances* to those that would be affected by the delete."""
    return [
        inst
        for inst in instances
        if _instance_serves_version(inst, name, version, newest_version)
    ]


def _validate_artifact_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=(
                "Artifact name must be 1-255 characters and contain only "
                "lowercase alphanumeric characters, hyphens, underscores, or dots."
            ),
        )


def _reject_latest_alias(version: str) -> None:
    if version.lower() == "latest":
        raise HTTPException(
            status_code=422,
            detail=(
                "'latest' is a reserved alias and cannot be deleted; "
                "delete a concrete version tag instead."
            ),
        )


def _upstream_error(action: str, status: int, body: object) -> HTTPException:
    detail = body if isinstance(body, str) else str(body)
    return HTTPException(
        status_code=502,
        detail=f"Data Repository {action} failed [{status}]: {detail}",
    )


class CatalogDeleteService:
    """Harbor-first delete + catalog unregister for models (S-048).

    Uses only the model category — the WebUI catalog lists models. Dataset
    deletion remains a Data Repository API concern (D-019).
    """

    def __init__(self, oci: OciPushClient, data_repo: DataRepoClient) -> None:
        self._oci = oci
        self._data_repo = data_repo

    async def delete_version(self, name: str, version: str) -> None:
        """Delete one model version: Harbor first, then unregister.

        Raises
        ------
        HTTPException
            422 for an invalid *name* or the reserved ``latest`` alias,
            404 when the model or version is unknown, 409 when a running
            instance serves the version, 502 on Harbor or upstream failure.
        """
        _validate_artifact_name(name)
        _reject_latest_alias(version)

        versions, newest = await self._fetch_versions(name)
        record = next(
            (v for v in versions if v.get("version") == version),
            None,
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"Version '{version}' was not found for model '{name}'.",
            )

        await self._guard(name, version, newest)

        harbor_ref = record["harbor_ref"]
        await self._delete_tag_or_502(harbor_ref, version)

        status, body = await self._data_repo.delete(
            f"/api/models/{name}/versions/{version}"
        )
        if status not in (204, 404):
            raise _upstream_error("version unregister", status, body)

    async def delete_artifact(self, name: str) -> DeleteArtifactResult:
        """Delete every version of a model, then the artifact row.

        Returns per-version results: versions whose Harbor delete succeeded
        are unregistered individually; when every version is clean the
        artifact row is removed and a best-effort repository delete is
        attempted. Any failure leaves the artifact row in place so the
        operator can retry.
        """
        _validate_artifact_name(name)

        versions, newest = await self._fetch_versions(name)
        await self._guard(name, version=None, newest=newest)

        deleted: list[str] = []
        failed: list[VersionDeleteFailure] = []
        for record in versions:
            version = record["version"]
            try:
                await self._delete_tag_or_502(record["harbor_ref"], version)
                deleted.append(version)
            except HTTPException as exc:
                failed.append(VersionDeleteFailure(version=version, detail=exc.detail))

        if failed:
            # Unregister the versions that were cleanly removed from Harbor,
            # keep the artifact row, and let the operator retry the rest.
            for version in list(deleted):
                status, body = await self._data_repo.delete(
                    f"/api/models/{name}/versions/{version}"
                )
                if status not in (204, 404):
                    deleted.remove(version)
                    failed.append(
                        VersionDeleteFailure(
                            version=version,
                            detail=f"unregister failed [{status}]: {body}",
                        )
                    )
            return DeleteArtifactResult(
                name=name,
                deleted=deleted,
                failed=failed,
                artifact_removed=False,
                harbor_repository_removed=False,
            )

        artifact_removed = False
        harbor_repository_removed = False
        status, body = await self._data_repo.delete(f"/api/models/{name}")
        if status in (204, 404):
            artifact_removed = True
            harbor_repository_removed = await self._oci.delete_repository(
                f"{HARBOR_PROJECT}/{name}"
            )
        else:
            failed.append(
                VersionDeleteFailure(
                    version="*",
                    detail=f"artifact unregister failed [{status}]: {body}",
                )
            )

        return DeleteArtifactResult(
            name=name,
            deleted=deleted,
            failed=failed,
            artifact_removed=artifact_removed,
            harbor_repository_removed=harbor_repository_removed,
        )

    # -- internals ----------------------------------------------------------

    async def _fetch_versions(self, name: str) -> tuple[list[dict], str | None]:
        """Fetch the version list; returns (versions, newest version or None).

        The Data Repository returns versions newest-first. 404 propagates as
        a model-not-found, everything else as 502.
        """
        status, body = await self._data_repo.get(f"/api/models/{name}/versions")
        if status == 404:
            raise HTTPException(
                status_code=404, detail=f"Model '{name}' was not found."
            )
        if status != 200:
            raise _upstream_error("version list", status, body)
        versions = body.get("versions", []) if isinstance(body, dict) else []
        newest = versions[0].get("version") if versions else None
        return versions, newest

    async def _guard(
        self,
        name: str,
        version: str | None,
        newest: str | None,
    ) -> None:
        """Refuse the delete when a running instance would be affected."""
        instances = await collect_running_instances()
        blockers = _blocking_instances(instances, name, version, newest)
        if blockers:
            listing = ", ".join(
                f"{inst.instance_id}@{inst.host_name}" for inst in blockers
            )
            target = f"version '{version}'" if version else "the model"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete {target} of model '{name}': "
                    f"served by running instances ({listing}). "
                    "Stop or undeploy the instances first."
                ),
            )

    async def _delete_tag_or_502(self, harbor_ref: str, version: str) -> None:
        """Delete one Harbor tag; 404 is tolerated, failures become 502."""
        try:
            await self._oci.delete_tag(parse_repo(harbor_ref), version)
        except OciPushError as exc:
            logger.error("Harbor delete failed for %s: %s", harbor_ref, exc)
            raise HTTPException(
                status_code=502,
                detail=f"Harbor delete failed for {harbor_ref}: {exc}",
            )


def build_catalog_delete_service() -> CatalogDeleteService:
    """Construct the delete relay with the app-level OCI client (S-048).

    Mirrors ``build_upload_service``: the OCI client is the same lazily
    created singleton (token cache stays warm), the Data Repository client
    is a fresh ``AioHttpDataRepo`` per call.
    """
    from app.services.uploads import AioHttpDataRepo, get_oci_client

    return CatalogDeleteService(oci=get_oci_client(), data_repo=AioHttpDataRepo())
