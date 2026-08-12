"""Scoped fault injection for the D-017 integration suite.

Every fault here mutates *session-scoped* stack state: the hosts table, the
data-repository subprocess, the host topology. The stack outlives the test
that breaks it, so a break that is undone by a plain statement further down
the test body is undone only on the happy path — one failed assertion in
between leaks the fault into every test that follows.

That is not hypothetical. On 2026-08-12 a failed assertion in
``test_edit_is_not_lost_while_the_host_is_unreachable`` skipped the API-key
restore; control answered 401 for the remaining 25 minutes of the session and
13 further tests timed out waiting for replicas that could never start.

So faults are context managers and the restore lives in ``finally``. Tests
must not call the underlying mutators directly.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fixtures.seed import update_host_api_key


def _host_real_key(stack: Any, host_name: str) -> str:
    """The API key the *running* host process authenticates with.

    Resolved from the stack rather than the DB row, which is exactly the
    value a rotation fault has to put back.
    """
    return stack.host_key(host_name.removeprefix("host-"))


@contextlib.contextmanager
def broken_host_api_key(
    stack: Any,
    host_id: str,
    host_name: str,
    *,
    bad_key: str = "wrong-key",
) -> Iterator[str]:
    """Make control's HTTP calls to *host_id* fail with 401 while WS stays up.

    The reconciler keeps its cached instance view (the WS channel is
    unaffected) but every pull/start/read against the host fails fast —
    the lever for the "host unreachable" and RECREATE-backoff paths.

    Yields the real key so a test can restore it early (to observe
    recovery) and still be safe if it never gets that far.
    """
    real_key = _host_real_key(stack, host_name)
    update_host_api_key(stack.db_env["control_db"], host_id, bad_key)
    try:
        yield real_key
    finally:
        update_host_api_key(stack.db_env["control_db"], host_id, real_key)


@contextlib.asynccontextmanager
async def dead_data_repo(stack: Any) -> AsyncIterator[None]:
    """Take the data-repository down for the duration of the block.

    SIGKILL, not terminate: a graceful shutdown keeps the port bound, so
    the resolver's TCP connect succeeds while the dying server never
    answers and the reconciler's action hangs past its timeouts instead of
    failing fast.

    Respawn on exit is conditional — a test may restore the service inside
    the block to observe recovery.
    """
    stack.data_repo.kill()
    try:
        yield
    finally:
        if stack.data_repo is None or not stack.data_repo.alive:
            await stack.respawn_data_repo()


@contextlib.asynccontextmanager
async def extra_host(stack: Any, letter: str) -> AsyncIterator[str]:
    """Add a host beyond the default two for the duration of the block.

    Removal is unconditional and covers a half-finished spawn: the host row
    is registered before the subprocess is up, so a readiness failure would
    otherwise leave a row pointing at nothing. The migration tests assume
    exactly two hosts (their "no target" scenario must find no third host),
    so a leaked host-c silently changes their outcome.
    """
    try:
        yield await stack.spawn_extra_host(letter)
    finally:
        stack.remove_extra_host(letter)
