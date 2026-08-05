#!/usr/bin/env python3
"""Step: upload_model — push a trained model artifact to Harbor and register it.

Reads MODEL_SOURCE_PATH, HARBOR_TARGET_REF, ARTIFACT_NAME, VERSION,
ARTIFACT_CATEGORY, and METADATA_PATH from the environment, pushes the source
artifact to Harbor via ORAS, registers the resulting version with the Data
Repository, and atomically updates /workspace/config/job.json.

Exit code 0 on success, non-zero on any failure.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import oras.defaults
import oras.oci
import requests
from harbor_oci_client import HarborError, OrasHelper
from harbor_oci_client.media_types import DATASET_CONFIG, MODEL_CONFIG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKSPACE_CONFIG = Path(os.environ.get("JOB_CONFIG", "/workspace/config/job.json"))
WORKSPACE_OUTPUT = Path(os.environ.get("WORKSPACE_OUTPUT", "/workspace/output"))
DEFAULT_HARBOR_OPERATION_TIMEOUT_SECONDS = 300
DEFAULT_REGISTRATION_TIMEOUT_SECONDS = 60

# Categories supported by the Data Repository registration endpoints.
SUPPORTED_CATEGORIES = ("model", "dataset")

# OCI config media types per category (spec §2.1).
_CONFIG_MEDIA_TYPES = {"model": MODEL_CONFIG, "dataset": DATASET_CONFIG}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StepError(Exception):
    """Base error for step failures. Exit code is always 1."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__(message)


class MissingEnvError(StepError):
    """A required environment variable is not set."""


class PathValidationError(StepError):
    """MODEL_SOURCE_PATH is outside the allowed workspace path."""


class SourceNotFoundError(StepError):
    """MODEL_SOURCE_PATH does not exist or contains no files."""


class ArtifactCategoryError(StepError):
    """ARTIFACT_CATEGORY is not a supported artifact category."""


class MetadataError(StepError):
    """The metadata file exists but cannot be read or is not a JSON object."""


class PushError(StepError):
    """Harbor ORAS push failed."""


class RegistrationError(StepError):
    """Data Repository registration call failed."""


class ConfigUpdateError(StepError):
    """Failed to update job.json atomically."""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def validate_category(category: str) -> str:
    """Validate ARTIFACT_CATEGORY against the categories the platform supports."""
    if category not in SUPPORTED_CATEGORIES:
        raise ArtifactCategoryError(
            f"ARTIFACT_CATEGORY must be one of {list(SUPPORTED_CATEGORIES)}, "
            f"got {category!r}"
        )
    return category


def validate_source_path(path: Path) -> Path:
    """Resolve and validate MODEL_SOURCE_PATH is under /workspace/output/.

    Returns the resolved absolute path on success.
    Raises PathValidationError if the path escapes the workspace.
    """
    resolved = path.resolve()
    try:
        resolved.relative_to(WORKSPACE_OUTPUT.resolve())
    except ValueError:
        raise PathValidationError(
            f"MODEL_SOURCE_PATH ({path}) must be under /workspace/output/ "
            f"(got resolved path: {resolved})"
        )
    return resolved


def require_source_artifact(source_path: Path) -> None:
    """Ensure the resolved source exists and holds at least one file.

    An empty source would otherwise push successfully and seal an empty,
    immutable version in the Data Repository.
    """
    if not source_path.exists():
        raise SourceNotFoundError(f"MODEL_SOURCE_PATH does not exist: {source_path}")
    if count_files(source_path) == 0:
        raise SourceNotFoundError(
            "MODEL_SOURCE_PATH contains no files, refusing to upload an empty "
            f"artifact: {source_path}"
        )


# ---------------------------------------------------------------------------
# Metadata aggregation
# ---------------------------------------------------------------------------


def load_metadata_file(metadata_path: Path | None) -> dict:
    """Load and validate the optional METADATA_PATH JSON file.

    Returns an empty dict when no path is provided or the file is absent. The
    step executor always injects METADATA_PATH, but the train step only
    optionally writes upload-metadata.json, so an absent file is not an error.
    Raises MetadataError if the file exists but is unreadable or does not
    contain a JSON object.
    """
    if metadata_path is None:
        return {}

    if not metadata_path.is_file():
        logger.warning(
            "METADATA_PATH %s does not exist; registering without file metadata",
            metadata_path,
        )
        return {}

    try:
        data = json.loads(metadata_path.read_text())
    except json.JSONDecodeError as exc:
        raise MetadataError(
            f"METADATA_PATH at {metadata_path} is not valid JSON: {exc}"
        ) from exc
    except OSError as exc:
        raise MetadataError(
            f"Failed to read METADATA_PATH at {metadata_path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise MetadataError(
            f"METADATA_PATH at {metadata_path} must contain a JSON object"
        )
    return data


def read_job_config(config_path: Path) -> dict:
    """Read and parse job.json, raising ConfigUpdateError on failure."""
    try:
        return json.loads(config_path.read_text())
    except FileNotFoundError:
        raise ConfigUpdateError(
            f"job.json not found at {config_path}. "
            "Solar Host should have created it before running this step."
        )
    except json.JSONDecodeError as exc:
        raise ConfigUpdateError(
            f"job.json at {config_path} is not valid JSON: {exc}"
        ) from exc


def resolve_source_path(job_config: dict) -> str | None:
    """Determine the model artifact path to upload from previous step results.

    Used when MODEL_SOURCE_PATH is not set. Reads
    ``steps.train.best_checkpoint_path`` (train pipeline) or
    ``steps.convert_model.output_path`` (conversion pipeline).

    Returns the resolved path string, or None when no source can be determined.
    """
    steps = job_config.get("steps") if isinstance(job_config.get("steps"), dict) else {}

    train = steps.get("train")
    if isinstance(train, dict) and train.get("best_checkpoint_path"):
        return train["best_checkpoint_path"]

    convert = steps.get("convert_model")
    if isinstance(convert, dict) and convert.get("output_path"):
        return convert["output_path"]

    return None


def aggregate_metadata(metadata_file: dict, job_config: dict) -> dict:
    """Combine upload-metadata.json with job.json into a registration payload.

    The metadata file is authoritative for the top-level sections it defines
    (description, training_config, model_config, eval_metrics, lineage). The
    job manifest contributes lineage defaults (source_trainer from job_id) and
    eval_metrics from the train step when the metadata file does not already
    provide them.
    """
    metadata = dict(metadata_file)

    steps = job_config.get("steps") if isinstance(job_config.get("steps"), dict) else {}

    # Lineage: source_trainer defaults to the job id.
    lineage = metadata.get("lineage")
    if not isinstance(lineage, dict):
        lineage = {}
    if not lineage.get("source_trainer") and job_config.get("job_id"):
        lineage["source_trainer"] = job_config["job_id"]
    if lineage:
        metadata["lineage"] = lineage

    # eval_metrics: fall back to the train step result when absent.
    if "eval_metrics" not in metadata:
        train = steps.get("train")
        if isinstance(train, dict) and isinstance(train.get("eval_metrics"), dict):
            metadata["eval_metrics"] = train["eval_metrics"]

    return metadata


# ---------------------------------------------------------------------------
# Harbor ORAS push
# ---------------------------------------------------------------------------


class HarborOperationTimeoutError(Exception):
    """Internal timeout raised while authenticating or pushing to Harbor."""


def harbor_hostname(harbor_url: str) -> str:
    """Return the registry host, preserving a non-default port."""
    parsed = urlparse(harbor_url)
    hostname = parsed.netloc if parsed.scheme else harbor_url.split("/", maxsplit=1)[0]
    hostname = hostname.rsplit("@", maxsplit=1)[-1]
    if not hostname:
        raise PushError(f"HARBOR_URL is invalid: {harbor_url!r}")
    return hostname


def validate_target_ref(harbor_ref: str, harbor_url: str) -> None:
    """Ensure HARBOR_TARGET_REF points at the registry we authenticate against.

    ORAS logs in to the HARBOR_URL host but pushes to the host embedded in the
    reference. A mismatch would otherwise surface as an opaque ORAS failure.
    """
    hostname = harbor_hostname(harbor_url)
    ref_host, separator, remainder = harbor_ref.partition("/")
    if not separator or not remainder:
        raise PushError(
            f"HARBOR_TARGET_REF ({harbor_ref}) must be a full OCI reference, "
            f"e.g. {hostname}/supernova/my-model:v1"
        )
    if ref_host != hostname:
        raise PushError(
            f"HARBOR_TARGET_REF ({harbor_ref}) targets registry {ref_host!r}, "
            f"but HARBOR_URL points at {hostname!r}"
        )


def harbor_operation_timeout_seconds() -> int:
    """Read and validate the bounded ORAS login/push timeout."""
    raw_value = os.environ.get("HARBOR_OPERATION_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_HARBOR_OPERATION_TIMEOUT_SECONDS

    try:
        timeout_seconds = int(raw_value)
    except ValueError as exc:
        raise PushError(
            "HARBOR_OPERATION_TIMEOUT_SECONDS must be a positive integer"
        ) from exc

    if timeout_seconds < 1:
        raise PushError("HARBOR_OPERATION_TIMEOUT_SECONDS must be a positive integer")
    return timeout_seconds


@contextmanager
def bounded_harbor_operation(timeout_seconds: int):
    """Interrupt a blocking ORAS login or push after timeout_seconds.

    On platforms without SIGALRM (e.g. Windows) the timeout is a no-op and the
    operation runs unbounded, matching the behavior of the download steps.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def raise_timeout(_signum: int, _frame: object) -> None:
        raise HarborOperationTimeoutError

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def validate_layer_title(title: str) -> None:
    """Validate a layer title against the artifact layout contract (spec §2.3).

    Titles must be relative POSIX paths, free of ``.`` and ``..`` segments,
    a leading slash, or a drive letter. Raises PushError otherwise.
    """
    if not title:
        raise PushError("Layer title must not be empty")
    if title.startswith("/") or re.match(r"^[A-Za-z]:", title):
        raise PushError(f"Layer title must be a relative path, got {title!r}")
    segments = title.split("/")
    if any(segment in (".", "..") for segment in segments):
        raise PushError(
            f"Layer title must not contain '.' or '..' segments, got {title!r}"
        )


def collect_artifact_files(source_path: Path) -> list[tuple[Path, str]]:
    """Return ``(absolute path, artifact-relative title)`` pairs for a source.

    A directory source is walked recursively; symlinks are skipped (matching
    ``compute_dir_size``). A single-file source yields one entry titled
    after the file. Titles are validated by :func:`build_flat_manifest`
    before anything is pushed.
    """
    if source_path.is_file():
        return [(source_path, source_path.name)]

    entries: list[tuple[Path, str]] = []
    for entry in sorted(source_path.rglob("*")):
        if entry.is_symlink() or not entry.is_file():
            continue
        entries.append((entry, entry.relative_to(source_path).as_posix()))
    return entries


def build_flat_manifest(
    config_path: Path,
    files: list[tuple[Path, str]],
    category: str,
) -> dict:
    """Assemble the OCI manifest for a flat artifact (spec §2.1).

    One layer per file; the layer digest is the sha256 of the exact file
    bytes and the artifact-relative POSIX path is carried in
    ``org.opencontainers.image.title``. The config blob keeps the
    SuperNova media type for the category.

    Every title is validated against spec §2.3 (relative, no ``.``/``..``
    segments, unique) before the manifest is built — the same checks Solar
    Control's relay and Solar Host's digest verification enforce.
    """
    if category not in _CONFIG_MEDIA_TYPES:
        raise PushError(
            f"Unknown category {category!r}; expected one of "
            f"{sorted(_CONFIG_MEDIA_TYPES)}"
        )

    seen: set[str] = set()
    for _path, title in files:
        validate_layer_title(title)
        if title in seen:
            raise PushError(f"Duplicate layer title: {title!r}")
        seen.add(title)

    manifest = oras.oci.NewManifest()
    conf, _ = oras.oci.ManifestConfig(str(config_path))
    conf["mediaType"] = _CONFIG_MEDIA_TYPES[category]
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for path, title in files:
        layer = oras.oci.NewLayer(str(path))
        layer["annotations"] = {
            oras.defaults.annotation_title: title,
            "org.opencontainers.image.created": created,
        }
        manifest["layers"].append(layer)

    manifest["config"] = conf
    manifest["annotations"] = {"org.opencontainers.image.created": created}
    return manifest


def push_to_harbor(
    harbor_ref: str,
    source_path: Path,
    config_path: Path,
    category: str,
    harbor_url: str,
    username: str,
    password: str,
    timeout_seconds: int = DEFAULT_HARBOR_OPERATION_TIMEOUT_SECONDS,
) -> str:
    """Push the source artifact to Harbor as a flat OCI artifact (spec §2.1).

    One OCI layer per file (digest = sha256 of the raw file bytes, title =
    the artifact-relative POSIX path) plus a config blob carrying the
    SuperNova media type for the category. ``OrasHelper.push`` cannot be
    used here: it basenames layer titles (losing nested paths) and emits a
    default OCI config, so the manifest is assembled explicitly with
    ``oras.oci`` — the same structure ``push_custom`` used, minus the tar.
    Raises PushError on any failure.
    """
    hostname = harbor_hostname(harbor_url)
    files = collect_artifact_files(source_path)
    manifest = build_flat_manifest(config_path, files, category)
    logger.info("Pushing %d files → %s (host=%s)", len(files), harbor_ref, hostname)

    try:
        with bounded_harbor_operation(timeout_seconds):
            oras = OrasHelper(
                hostname=hostname,
                username=username,
                password=password,
            )
            # OrasHelper wraps oras-py's OrasClient but does not re-export
            # its upload methods; the underlying client is reached directly
            # for the config/blob/manifest upload sequence.
            client = oras._client  # type: ignore[attr-defined]
            container = client.get_container(harbor_ref)
            client._check_200_response(  # type: ignore[attr-defined]
                client.upload_blob(str(config_path), container, manifest["config"])
            )
            for (path, _title), layer in zip(files, manifest["layers"]):
                client._check_200_response(  # type: ignore[attr-defined]
                    client.upload_blob(str(path), container, layer)
                )
            response = client.upload_manifest(manifest, container)
            client._check_200_response(response)  # type: ignore[attr-defined]

        digest = response.headers.get("Docker-Content-Digest", "")
        if not digest:
            raise PushError(f"ORAS push returned no digest for {harbor_ref}")
        logger.info("Push complete: %d files, digest=%s", len(files), digest)
        return digest
    except HarborOperationTimeoutError as exc:
        raise PushError(
            f"ORAS operation timed out after {timeout_seconds} seconds for {harbor_ref}"
        ) from exc
    except HarborError as exc:
        raise PushError(
            f"ORAS push failed for {harbor_ref}: {exc}",
            detail=getattr(exc, "detail", str(exc)),
        ) from exc
    except StepError:
        raise
    except Exception as exc:
        raise PushError(
            f"ORAS push failed for {harbor_ref}: {exc}",
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Data Repository registration
# ---------------------------------------------------------------------------


def registration_endpoint(data_repo_url: str, artifact_name: str, category: str) -> str:
    """Return the version-registration URL for the artifact category."""
    validate_category(category)
    return f"{data_repo_url.rstrip('/')}/api/{category}s/{artifact_name}/versions"


def register_version(
    data_repo_url: str,
    artifact_name: str,
    category: str,
    *,
    harbor_ref: str,
    version: str | None,
    digest: str,
    metadata: dict,
    size_bytes: int | None = None,
) -> dict:
    """Register the pushed artifact version with the Data Repository.

    Calls POST /api/{models|datasets}/{artifact_name}/versions and returns the
    JSON response. Raises RegistrationError on HTTP errors, connection
    failures, or non-JSON responses.

    ``size_bytes`` is the sum of the pushed file sizes. It is sent
    explicitly because the artifact is now stored flat (spec §2.1), so the
    stored bytes equal the source bytes; without it the Data Repository
    would fall back to the manifest HEAD content length, which is the size
    of the manifest JSON, not the artifact.
    """
    url = registration_endpoint(data_repo_url, artifact_name, category)
    payload: dict = {
        "harbor_ref": harbor_ref,
        "checksum": digest,
        "metadata": metadata,
    }
    if version:
        payload["version"] = version
    if size_bytes is not None:
        payload["size_bytes"] = size_bytes

    logger.info("Registering %s version via %s", artifact_name, url)

    try:
        resp = requests.post(
            url, json=payload, timeout=DEFAULT_REGISTRATION_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise RegistrationError(
            f"Failed to reach Data Repository at {url}: {exc}"
        ) from exc

    if resp.status_code == 409:
        raise RegistrationError(
            f"Data Repository rejected registration for {artifact_name}: "
            "version already exists or category conflict",
            detail=resp.text[:500],
        )
    if resp.status_code == 404:
        raise RegistrationError(
            f"Data Repository could not verify {harbor_ref} in Harbor",
            detail=f"POST {url} returned 404",
        )
    if not resp.ok:
        raise RegistrationError(
            f"Data Repository returned {resp.status_code} for {artifact_name}",
            detail=resp.text[:500],
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise RegistrationError(
            f"Data Repository returned non-JSON response for {artifact_name}",
            detail=resp.text[:500],
        ) from exc

    if not isinstance(data, dict):
        raise RegistrationError(
            f"Data Repository returned an unexpected response for {artifact_name}",
            detail=resp.text[:500],
        )
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def count_files(path: Path) -> int:
    """Return the number of regular files in the artifact at path."""
    if path.is_file():
        return 1
    if not path.is_dir():
        return 0
    return sum(1 for entry in path.rglob("*") if entry.is_file())


def compute_dir_size(path: Path) -> int:
    """Return total size in bytes of the artifact at path.

    For a directory, sums all regular files recursively. For a single file,
    returns that file's size.
    """
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    if not path.is_dir():
        return 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def write_config_json(config_path: Path, config: dict) -> None:
    """Atomically write config to config_path via a temp file and rename."""
    config_dir = config_path.parent
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=config_dir,
        prefix=".job-",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        try:
            json.dump(config, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    # os.replace is atomic on both POSIX and Windows and overwrites the
    # destination, unlike Path.rename which fails on Windows when the target
    # already exists.
    os.replace(tmp_path, config_path)


def update_job_config(
    config_path: Path,
    *,
    harbor_ref: str,
    digest: str,
    size_bytes: int,
    version: str | None,
    registration: dict,
) -> None:
    """Atomically update job.json with the upload_model step result.

    Reads the existing job.json, adds/overwrites steps.upload_model, writes to
    a temp file, and renames to replace the original.
    """
    logger.info("Updating job config: %s", config_path)

    config = read_job_config(config_path)

    if not digest:
        raise ConfigUpdateError("upload_model result requires a digest")

    step_result: dict = {
        "status": "completed",
        "harbor_ref": harbor_ref,
        "digest": digest,
        "size_bytes": size_bytes,
        "registration": registration,
    }
    if version:
        step_result["version"] = version

    if "steps" not in config:
        config["steps"] = {}
    if not isinstance(config["steps"], dict):
        raise ConfigUpdateError(
            f"job.json at {config_path} has a non-object steps field"
        )
    config["steps"]["upload_model"] = step_result

    write_config_json(config_path, config)
    logger.info("job.json updated successfully with step result")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point. Reads env, validates, pushes, registers, updates config."""
    setup_logging()

    # --- Read environment variables ---
    model_source_path = os.environ.get("MODEL_SOURCE_PATH")

    harbor_target_ref = os.environ.get("HARBOR_TARGET_REF")
    if not harbor_target_ref:
        raise MissingEnvError(
            "HARBOR_TARGET_REF is required "
            "(e.g. imgrepo.damit.hu/supernova/iris-osl:v4)"
        )

    artifact_name = os.environ.get("ARTIFACT_NAME")
    if not artifact_name:
        raise MissingEnvError("ARTIFACT_NAME is required (e.g. iris-osl)")

    version = os.environ.get("VERSION")

    artifact_category = validate_category(os.environ.get("ARTIFACT_CATEGORY", "model"))

    metadata_path_raw = os.environ.get("METADATA_PATH")
    metadata_path = Path(metadata_path_raw) if metadata_path_raw else None

    data_repo_url = os.environ.get("DATA_REPOSITORY_URL")
    if not data_repo_url:
        raise MissingEnvError(
            "DATA_REPOSITORY_URL is required (e.g. http://data-repository:8000)"
        )

    harbor_url = os.environ.get("HARBOR_URL")
    if not harbor_url:
        raise MissingEnvError("HARBOR_URL is required (e.g. https://imgrepo.damit.hu)")

    harbor_username = os.environ.get("HARBOR_USERNAME")
    if not harbor_username:
        raise MissingEnvError("HARBOR_USERNAME is required")

    harbor_password = os.environ.get("HARBOR_PASSWORD")
    if not harbor_password:
        raise MissingEnvError("HARBOR_PASSWORD is required")

    validate_target_ref(harbor_target_ref, harbor_url)

    # --- Resolve the source path (env var wins, else previous step result) ---
    if model_source_path:
        source_path = validate_source_path(Path(model_source_path))
        require_source_artifact(source_path)
        job_config = read_job_config(WORKSPACE_CONFIG)
    else:
        # Env var absent: read job.json to determine the source from the
        # previous step result (train best checkpoint or convert output).
        job_config = read_job_config(WORKSPACE_CONFIG)
        resolved_source = resolve_source_path(job_config)
        if not resolved_source:
            raise MissingEnvError(
                "MODEL_SOURCE_PATH is required, or job.json must contain "
                "steps.train.best_checkpoint_path or steps.convert_model.output_path "
                "to determine what to upload"
            )
        source_path = validate_source_path(Path(resolved_source))
        require_source_artifact(source_path)

    logger.info("MODEL_SOURCE_PATH=%s", source_path)
    logger.info("HARBOR_TARGET_REF=%s", harbor_target_ref)
    logger.info("ARTIFACT_NAME=%s", artifact_name)
    logger.info("VERSION=%s", version)
    logger.info("ARTIFACT_CATEGORY=%s", artifact_category)
    logger.info("METADATA_PATH=%s", metadata_path)
    logger.info("DATA_REPOSITORY_URL=%s", data_repo_url)
    logger.info("HARBOR_URL=%s", harbor_url)

    # --- Aggregate metadata from METADATA_PATH and job.json ---
    metadata = aggregate_metadata(load_metadata_file(metadata_path), job_config)
    logger.info("Aggregated metadata sections: %s", sorted(metadata.keys()))

    # --- Push the source artifact to Harbor via ORAS ---
    # The OCI config layer is staged inside the workspace and removed
    # afterwards so it does not linger in the shared config directory.
    config_dir = WORKSPACE_CONFIG.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    oci_dir = Path(tempfile.mkdtemp(dir=config_dir, prefix=".upload-oci-"))
    try:
        oci_config_path = oci_dir / "config.json"
        write_config_json(
            oci_config_path,
            {
                "artifact_type": artifact_category,
                "name": artifact_name,
                "version": version,
                "metadata": metadata,
            },
        )
        digest = push_to_harbor(
            harbor_ref=harbor_target_ref,
            source_path=source_path,
            config_path=oci_config_path,
            category=artifact_category,
            harbor_url=harbor_url,
            username=harbor_username,
            password=harbor_password,
            timeout_seconds=harbor_operation_timeout_seconds(),
        )
    finally:
        shutil.rmtree(oci_dir, ignore_errors=True)

    size_bytes = compute_dir_size(source_path)
    logger.info("Artifact size: %d bytes (sum of pushed files)", size_bytes)

    # --- Register the version with the Data Repository ---
    registration = register_version(
        data_repo_url,
        artifact_name,
        artifact_category,
        harbor_ref=harbor_target_ref,
        version=version,
        digest=digest,
        metadata=metadata,
        size_bytes=size_bytes,
    )
    logger.info(
        "Registered %s version %s (harbor_ref=%s)",
        artifact_name,
        registration.get("version"),
        registration.get("harbor_ref"),
    )

    # --- Atomically update job.json with step result ---
    try:
        update_job_config(
            config_path=WORKSPACE_CONFIG,
            harbor_ref=harbor_target_ref,
            digest=digest,
            size_bytes=size_bytes,
            version=version,
            registration=registration,
        )
    except StepError:
        # The version is already sealed in the Data Repository and cannot be
        # rolled back, so a plain re-run of this step will conflict. Surface
        # what was registered so the pipeline can be resumed by hand.
        logger.error(
            "Registered %s version %s (harbor_ref=%s) but failed to record it "
            "in job.json; re-running this step will fail with a version conflict",
            artifact_name,
            registration.get("version"),
            harbor_target_ref,
        )
        raise


def setup_logging() -> None:
    """Configure stdout logging at INFO level."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


if __name__ == "__main__":
    try:
        main()
    except StepError as exc:
        detail = f": {exc.detail}" if exc.detail else ""
        logger.error("%s%s", exc, detail)
        sys.exit(1)
    except Exception:
        logger.exception("Unexpected error in upload_model step")
        sys.exit(1)
