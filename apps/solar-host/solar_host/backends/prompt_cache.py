"""Per-instance prompt-cache directories for the GPU backends.

SGLang's HiCache file backend and vLLM's KV offloading filesystem tier both
persist their prompt cache in a host-owned directory. The host owns the location
(``settings.<backend>_prompt_cache_dir``), gives every instance its own
``<alias>-<instance_id>`` subdirectory, and hands the path to the engine through
an environment variable rather than the instance config, so an operator never has to
know where the host puts it. The subdirectory is deleted when the instance stops,
and leftovers from a dead host are swept at startup.

The lifecycle is identical for both backends, so it lives here once. Each backend
contributes only a :class:`PromptCacheBackend` descriptor: the ``settings``
attribute holding its root, the environment variable its engine reads, and the label
used in log messages.
"""

import logging
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solar_host.config import settings
from solar_host.naming import sanitize_alias_for_fs

logger = logging.getLogger(__name__)

# Teardown detaches a run's dir with an O(1) rename to `.trash-<uuid>`
# and a daemon thread purges it. Nothing else may be detached: the
# suffix guard and the trash prefix scope this to dirs the host created.
_TRASH_PREFIX = ".trash-"

# Both host-created dirs (`<alias>-<uuid4>`) and detached dirs
# (`.trash-<uuid4>`) end in this shape, and nothing else may be touched.
_CACHE_DIR_SUFFIX_RE = re.compile(
    r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PromptCacheBackend:
    """One backend's prompt-cache wiring.

    ``label`` is only for log messages; ``setting`` names the ``settings``
    attribute holding the root and ``env_var`` the variable the engine reads the
    per-instance directory from.
    """

    label: str
    setting: str
    env_var: str

    def root(self) -> Path | None:
        """The configured root; None when unset or blank."""
        raw = getattr(settings, self.setting, "").strip()
        if not raw:
            return None
        return Path(raw)

    def instance_dir(self, alias: str, instance_id: str) -> Path | None:
        """The prompt-cache dir this backend uses for an instance; None when no root is set."""
        root = self.root()
        if root is None:
            return None
        return root / f"{sanitize_alias_for_fs(alias)}-{instance_id}"


SGLANG_PROMPT_CACHE = PromptCacheBackend(
    label="SGLang",
    setting="sglang_prompt_cache_dir",
    env_var="SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR",
)

VLLM_PROMPT_CACHE = PromptCacheBackend(
    label="vLLM",
    setting="vllm_prompt_cache_dir",
    env_var="VLLM_PROMPT_CACHE_DIR",
)

# Keyed on the instance config's backend_type, so a backend with no persistent
# prompt cache (llama.cpp, HuggingFace) is a no-op by absence.
BACKENDS: dict[str, PromptCacheBackend] = {
    "sglang": SGLANG_PROMPT_CACHE,
    "vllm": VLLM_PROMPT_CACHE,
}


def backend_for(config: Any) -> PromptCacheBackend | None:
    """The prompt-cache descriptor for *config*'s backend; None when it has none."""
    return BACKENDS.get(getattr(config, "backend_type", "") or "")


def _detach_dir(backend: PromptCacheBackend, cache_dir: Path) -> Path | None:
    """Rename *cache_dir* aside to a ``.trash-<uuid>`` sibling (O(1)); None when nothing was detached.

    Symlinks are skipped, never followed; ``FileNotFoundError`` is a no-op
    and other ``OSError`` is logged only — cleanup must never fail a stop.
    """
    trash_dir = cache_dir.with_name(f"{_TRASH_PREFIX}{uuid.uuid4()}")
    try:
        if cache_dir.is_symlink():
            return None
        cache_dir.rename(trash_dir)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "Cannot detach %s prompt cache dir %s: %s", backend.label, cache_dir, exc
        )
        return None
    return trash_dir


def detach_instance_prompt_cache(
    backend: PromptCacheBackend, alias: str, instance_id: str
) -> Path | None:
    """Move an instance's prompt-cache dir aside; None when there is nothing to detach."""
    cache_dir = backend.instance_dir(alias, instance_id)
    if cache_dir is None:
        return None
    return _detach_dir(backend, cache_dir)


def purge_in_background(trash_dirs: list[Path]) -> None:
    """rmtree the given trash dirs on a daemon thread; never blocks and never raises.

    Each dir is purged independently so one failure cannot strand the rest
    of the batch until the next boot, and each ``rmtree`` runs with a
    per-file ``onexc`` callback so one unreadable entry cannot strand the
    rest of a multi-GB tree; each dir logs one bounded warning whose count
    only includes real failures — an entry already gone
    (``FileNotFoundError``) is a clean purge and is not counted. Daemon on
    purpose, so shutdown never waits on multi-GB unlinks.
    """
    if not trash_dirs:
        return

    def _purge_one(trash_dir: Path) -> None:
        failed = 0
        first_failure: str | None = None

        def _record(func, path, exc) -> None:
            # Already gone: another actor removed it, or the whole dir was
            # never there. That is a clean purge, not something left behind
            # (mirrors models_manager.delete_model_files).
            if isinstance(exc, FileNotFoundError):
                logger.debug("Purge entry already gone: %s", path)
                return
            nonlocal failed, first_failure
            failed += 1
            if first_failure is None:
                first_failure = f"{path}: {exc}"

        try:
            # onexc: a single unreadable/sticky entry must not strand
            # the rest of a multi-GB tree until the next boot.
            shutil.rmtree(trash_dir, onexc=_record)
        except Exception:
            logger.exception(
                "Unexpected error purging prompt cache trash dir %s", trash_dir
            )
            return
        if failed:
            logger.warning(
                "Purge of %s left %d entries behind; first: %s",
                trash_dir,
                failed,
                first_failure,
            )

    def _purge() -> None:
        for trash_dir in trash_dirs:
            _purge_one(trash_dir)

    threading.Thread(target=_purge, daemon=True).start()


def detach_all_prompt_cache_dirs(backend: PromptCacheBackend) -> list[Path]:
    """Rename every host-created prompt-cache dir under the root aside and return the trash paths.

    Boot-only pass, and it runs before ``init_clients``: no start of this
    host can be in flight while it scans. A surviving orphan child from a
    dead host may still hold one of these dirs — acceptable, because the
    cache is disposable and ``auto_restart_running_instances`` kills the
    child later in the same boot. Deliberately no live-instance inventory
    filter (deviation from issue #45; documented in the PR): every
    host-shaped dir is trash at boot. A child is detached iff it is a real
    directory (never a symlink or loose file) whose name ends in the uuid
    suffix every host-created dir carries (``<alias>-<instance_id>``); a
    name already starting with ``.trash-`` is collected as-is. No-op when
    the root is unset or missing; every probe is best-effort (logged,
    never fatal).
    """
    root = backend.root()
    if root is None:
        return []
    try:
        if not root.is_dir():
            return []
        # Snapshot before renaming: unlinking a readdir entry mid-iteration
        # can skip later siblings on Linux.
        children = list(root.iterdir())
    except OSError as exc:
        logger.warning(
            "Cannot scan %s prompt cache root %s: %s", backend.label, root, exc
        )
        return []
    detached: list[Path] = []
    for child in children:
        if _CACHE_DIR_SUFFIX_RE.search(child.name) is None:
            logger.debug("Skipping %s: not a host-created cache dir", child)
            continue
        try:
            if not child.is_dir() or child.is_symlink():
                continue
        except OSError as exc:
            logger.warning(
                "Cannot inspect %s prompt cache dir %s: %s", backend.label, child, exc
            )
            continue
        if child.name.startswith(_TRASH_PREFIX):
            detached.append(child)
            continue
        trash_dir = _detach_dir(backend, child)
        if trash_dir is not None:
            detached.append(trash_dir)
    return detached


def discard_orphan_prompt_caches(backend: PromptCacheBackend) -> None:
    """Boot cleanup for one backend: detach leftover dirs and purge them in the background."""
    purge_in_background(detach_all_prompt_cache_dirs(backend))


def discard_all_orphan_prompt_caches() -> None:
    """Boot cleanup: run the detach pass for every backend with a prompt cache."""
    for backend in BACKENDS.values():
        discard_orphan_prompt_caches(backend)


def discard_instance_prompt_cache_now(instance: Any) -> None:
    """Detach and purge *instance*'s prompt-cache dir; never raises.

    Unconditional: the caller owns the decision that no live run needs the
    dir. Teardown paths must go through the process manager's live-run guard
    instead. A backend without a persistent prompt cache is a no-op.
    """
    backend = backend_for(instance.config)
    if backend is None:
        return
    try:
        trash_dir = detach_instance_prompt_cache(
            backend, instance.config.alias, instance.id
        )
        if trash_dir is not None:
            purge_in_background([trash_dir])
    except Exception:
        # Cleanup is best-effort; a helper regression must not fail a
        # stop, purge or delete.
        logger.exception(
            "Failed to discard %s prompt cache dir for instance %s",
            backend.label,
            instance.id,
        )
