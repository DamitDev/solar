#!/usr/bin/env python3
"""Step: download_dataset — resolve and pull a dataset artifact into the job workspace.

Reads DATASET_URI and DATASET_OUTPUT_DIR from the environment, resolves the
artifact via Data Repository, pulls from Harbor via ORAS, and atomically
updates /workspace/config/job.json.

Exit code 0 on success, non-zero on any failure.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests
from harbor_oci_client import HarborError, OrasHelper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKSPACE_CONFIG = Path(os.environ.get("JOB_CONFIG", "/workspace/config/job.json"))
WORKSPACE_DATA = Path(os.environ.get("WORKSPACE_DATA", "/workspace/data"))
DEFAULT_HARBOR_OPERATION_TIMEOUT_SECONDS = 300


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
    """DATASET_OUTPUT_DIR is outside the allowed workspace path."""


class ResolveError(StepError):
    """Data Repository resolve call failed."""


class PullError(StepError):
    """Harbor ORAS pull failed."""


class DestinationError(StepError):
    """DATASET_OUTPUT_DIR cannot safely receive a downloaded artifact."""


class ConfigUpdateError(StepError):
    """Failed to update job.json atomically."""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def validate_output_path(path: Path) -> Path:
    """Resolve and validate DATASET_OUTPUT_DIR is under /workspace/data/.

    Returns the resolved absolute path on success.
    Raises PathValidationError if the path escapes the workspace.
    """
    resolved = path.resolve()
    try:
        resolved.relative_to(WORKSPACE_DATA.resolve())
    except ValueError:
        raise PathValidationError(
            f"DATASET_OUTPUT_DIR ({path}) must be under /workspace/data/ "
            f"(got resolved path: {resolved})"
        )
    return resolved


# ---------------------------------------------------------------------------
# Data Repository resolution
# ---------------------------------------------------------------------------


def resolve_dataset_uri(dataset_uri: str, data_repo_url: str) -> dict:
    """Resolve a repo:// URI via Data Repository.

    Calls GET /api/resolve?uri=<dataset_uri> and returns the JSON response.
    The response must include: harbor_ref, record_count, and format.

    Raises ResolveError on HTTP errors, connection failures, or missing fields.
    """
    url = f"{data_repo_url.rstrip('/')}/api/resolve"
    logger.info("Resolving %s via %s", dataset_uri, url)

    try:
        resp = requests.get(url, params={"uri": dataset_uri}, timeout=30)
    except requests.RequestException as exc:
        raise ResolveError(f"Failed to reach Data Repository at {url}: {exc}") from exc

    if resp.status_code == 404:
        raise ResolveError(
            f"Artifact not found in Data Repository: {dataset_uri}",
            detail=f"GET {url} returned 404",
        )

    if not resp.ok:
        raise ResolveError(
            f"Data Repository returned {resp.status_code} for {dataset_uri}",
            detail=resp.text[:500],
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise ResolveError(
            f"Data Repository returned non-JSON response for {dataset_uri}",
            detail=resp.text[:500],
        ) from exc

    # Validate the complete step-result contract before starting a transfer.
    harbor_ref = data.get("harbor_ref")
    record_count = data.get("record_count")
    dataset_format = data.get("format")
    missing_fields = [
        name
        for name, value in {
            "harbor_ref": harbor_ref,
            "record_count": record_count,
            "format": dataset_format,
        }.items()
        if value is None or value == ""
    ]
    if missing_fields:
        raise ResolveError(
            "Data Repository response missing required field(s) for "
            f"{dataset_uri}: {', '.join(missing_fields)}",
            detail=json.dumps(data),
        )
    if (
        not isinstance(record_count, int)
        or isinstance(record_count, bool)
        or record_count < 0
    ):
        raise ResolveError(
            f"Data Repository returned invalid record_count for {dataset_uri}",
            detail=json.dumps(data),
        )
    if not isinstance(dataset_format, str):
        raise ResolveError(
            f"Data Repository returned invalid format for {dataset_uri}",
            detail=json.dumps(data),
        )

    return {
        "harbor_ref": harbor_ref,
        "name": data.get("name"),
        "version": data.get("version"),
        "category": data.get("category"),
        "record_count": record_count,
        "format": dataset_format,
    }


# ---------------------------------------------------------------------------
# Harbor ORAS pull
# ---------------------------------------------------------------------------


class HarborOperationTimeoutError(Exception):
    """Internal timeout raised while authenticating or pulling from Harbor."""


def harbor_hostname(harbor_url: str) -> str:
    """Return the registry host, preserving a non-default port."""
    parsed = urlparse(harbor_url)
    hostname = parsed.netloc if parsed.scheme else harbor_url.split("/", maxsplit=1)[0]
    hostname = hostname.rsplit("@", maxsplit=1)[-1]
    if not hostname:
        raise PullError(f"HARBOR_URL is invalid: {harbor_url!r}")
    return hostname


def harbor_operation_timeout_seconds() -> int:
    """Read and validate the bounded ORAS login/pull timeout."""
    raw_value = os.environ.get("HARBOR_OPERATION_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_HARBOR_OPERATION_TIMEOUT_SECONDS

    try:
        timeout_seconds = int(raw_value)
    except ValueError as exc:
        raise PullError(
            "HARBOR_OPERATION_TIMEOUT_SECONDS must be a positive integer"
        ) from exc

    if timeout_seconds < 1:
        raise PullError("HARBOR_OPERATION_TIMEOUT_SECONDS must be a positive integer")
    return timeout_seconds


@contextmanager
def bounded_harbor_operation(timeout_seconds: int):
    """Interrupt a blocking ORAS login or pull after timeout_seconds."""
    if threading.current_thread() is not threading.main_thread():
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


def pull_from_harbor(
    harbor_ref: str,
    output_path: Path,
    harbor_url: str,
    username: str,
    password: str,
    timeout_seconds: int = DEFAULT_HARBOR_OPERATION_TIMEOUT_SECONDS,
) -> None:
    """Pull an OCI artifact from Harbor via ORAS into output_path atomically.

    Pulls into a sibling temporary directory, then renames it into place only
    after a non-empty transfer succeeds. Raises PullError on any failure.
    """
    hostname = harbor_hostname(harbor_url)

    logger.info("Pulling %s → %s (host=%s)", harbor_ref, output_path, hostname)

    if output_path.exists():
        raise DestinationError(
            "DATASET_OUTPUT_DIR already exists and will not be overwritten: "
            f"{output_path}"
        )

    staging_path: Path | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path = Path(
            tempfile.mkdtemp(
                dir=output_path.parent,
                prefix=f".{output_path.name}.download-",
            )
        )
        with bounded_harbor_operation(timeout_seconds):
            oras = OrasHelper(
                hostname=hostname,
                username=username,
                password=password,
            )
            oras.pull(harbor_ref, outdir=str(staging_path))

        if count_files(staging_path) == 0:
            raise PullError(f"ORAS pull returned no files for {harbor_ref}")

        staging_path.replace(output_path)
    except HarborOperationTimeoutError as exc:
        raise PullError(
            f"ORAS operation timed out after {timeout_seconds} seconds for {harbor_ref}"
        ) from exc
    except HarborError as exc:
        raise PullError(
            f"ORAS pull failed for {harbor_ref}: {exc}",
            detail=getattr(exc, "detail", str(exc)),
        ) from exc
    except StepError:
        raise
    except Exception as exc:
        raise PullError(
            f"ORAS pull failed for {harbor_ref}: {exc}",
            detail=str(exc),
        ) from exc
    finally:
        if staging_path is not None and staging_path.exists():
            shutil.rmtree(staging_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def count_files(path: Path) -> int:
    """Count regular files recursively under path (for logging)."""
    if not path.is_dir():
        return 0
    return sum(1 for _ in path.rglob("*") if _.is_file())


# ---------------------------------------------------------------------------
# Atomic job.json update
# ---------------------------------------------------------------------------


def update_job_config(
    config_path: Path,
    *,
    dataset_dir: str,
    source_uri: str,
    harbor_ref: str,
    record_count: int,
    dataset_format: str,
) -> None:
    """Atomically update job.json with the download_dataset step result.

    Reads the existing job.json, adds/overwrites steps.download_dataset,
    writes to a temp file, and renames to replace the original.
    """
    logger.info("Updating job config: %s", config_path)

    # Read existing config (fail if missing — Solar Host must have created it)
    try:
        config = json.loads(config_path.read_text())
    except FileNotFoundError:
        raise ConfigUpdateError(
            f"job.json not found at {config_path}. "
            "Solar Host should have created it before running this step."
        )
    except json.JSONDecodeError as exc:
        raise ConfigUpdateError(
            f"job.json at {config_path} is not valid JSON: {exc}"
        ) from exc

    if record_count < 0:
        raise ConfigUpdateError(
            "download_dataset result requires a non-negative record_count"
        )
    if not dataset_format:
        raise ConfigUpdateError("download_dataset result requires a format")

    # Build step result
    step_result: dict[str, str | int] = {
        "status": "completed",
        "dataset_dir": dataset_dir,
        "source_uri": source_uri,
        "harbor_ref": harbor_ref,
        "record_count": record_count,
        "format": dataset_format,
    }

    # Update config
    if "steps" not in config:
        config["steps"] = {}
    if not isinstance(config["steps"], dict):
        raise ConfigUpdateError(
            f"job.json at {config_path} has a non-object steps field"
        )
    config["steps"]["download_dataset"] = step_result

    # Atomic write: write to a temp file in the same directory, then rename
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

    # Atomic rename (POSIX: rename is atomic if source and target
    # are on the same filesystem)
    tmp_path.rename(config_path)
    logger.info("job.json updated successfully with step result")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point. Reads env, validates, resolves, pulls, updates config."""
    setup_logging()

    # --- Read environment variables ---
    dataset_uri = os.environ.get("DATASET_URI")
    if not dataset_uri:
        raise MissingEnvError(
            "DATASET_URI is required (e.g. repo://iris-tickets:2026-03)"
        )

    dataset_output_dir = os.environ.get("DATASET_OUTPUT_DIR")
    if not dataset_output_dir:
        raise MissingEnvError(
            "DATASET_OUTPUT_DIR is required (e.g. /workspace/data/tickets-dataset)"
        )

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

    # Validate path safety
    output_path = validate_output_path(Path(dataset_output_dir))

    logger.info("DATASET_URI=%s", dataset_uri)
    logger.info("DATASET_OUTPUT_DIR=%s", output_path)
    logger.info("DATA_REPOSITORY_URL=%s", data_repo_url)
    logger.info("HARBOR_URL=%s", harbor_url)

    # --- Resolve the dataset URI via Data Repository ---
    resolved = resolve_dataset_uri(dataset_uri, data_repo_url)
    harbor_ref = resolved["harbor_ref"]
    record_count = resolved["record_count"]
    dataset_format = resolved["format"]
    logger.info(
        "Resolved: harbor_ref=%s record_count=%s format=%s",
        harbor_ref,
        record_count,
        dataset_format,
    )

    # --- Pull the artifact from Harbor into the workspace ---
    pull_from_harbor(
        harbor_ref=harbor_ref,
        output_path=output_path,
        harbor_url=harbor_url,
        username=harbor_username,
        password=harbor_password,
        timeout_seconds=harbor_operation_timeout_seconds(),
    )
    logger.info("Pull complete: %d files in %s", count_files(output_path), output_path)

    # --- Atomically update job.json with step result ---
    try:
        update_job_config(
            config_path=WORKSPACE_CONFIG,
            dataset_dir=str(output_path),
            source_uri=dataset_uri,
            harbor_ref=harbor_ref,
            record_count=record_count,
            dataset_format=dataset_format,
        )
    except Exception:
        # The dataset is unusable without a matching completed step record.
        # Remove only the directory created by this invocation.
        shutil.rmtree(output_path, ignore_errors=True)
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
        logger.exception("Unexpected error in download_dataset step")
        sys.exit(1)
