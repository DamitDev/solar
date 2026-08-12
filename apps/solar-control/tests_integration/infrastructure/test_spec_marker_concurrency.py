"""infrastructure: an accepted edit survives a concurrent status write.

``status.spec_changed_at`` is the only record that an accepted edit still has
to reach the replicas. While it is set the reconciler loads each replica's
full configuration and compares it against the new spec; once it is cleared
the edit counts as delivered. Erasing it therefore does not fail anything —
it drops the edit, and the intent goes on reporting ``ready`` with every
replica ``updated`` while those replicas keep serving the previous config.

Two writers touch the row. ``update_intent`` (the PUT) stamps the marker;
``update_status`` (every reconcile tick, per intent) rewrites the whole status
document and so would overwrite it. The second one is passed the
``spec_changed_at`` its pass reconciled against, and preserves the stored
marker when the two differ — but it compares against a value read inside its
own transaction, so the guard only holds if no edit can commit between that
read and the write.

Driven against a real PostgreSQL because the property under test *is*
transaction isolation; nothing weaker distinguishes a serialized read from an
unserialized one. Observed in CI on 2026-08-12: a backend edit returned 200,
the marker never appeared, the reconciler never opened its deep-compare
window, and the replica kept the old config for the rest of the test.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import psycopg2
import pytest
import pytest_asyncio

pytestmark = pytest.mark.infrastructure

# Its own database, not the running control plane's: these rows would
# otherwise be reconciled by the live control process mid-test.
_SCRATCH_DB = "spec_marker_race"

_BACKEND = {"backend_type": "huggingface_classification", "max_length": 128}


class _PausingSession:
    """A real session that hands control back once, after its first ``get``.

    That is the instant the guard's comparison value is read, and the whole
    question is what may commit between then and the write.
    """

    def __init__(self, inner: Any, hook: Any) -> None:
        self._inner = inner
        self._hook = hook

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        row = await self._inner.get(*args, **kwargs)
        hook, self._hook = self._hook, None
        if hook is not None:
            await hook()
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _PausingSessionCM:
    def __init__(self, inner: Any, hook: Any) -> None:
        self._inner = inner
        self._hook = hook

    async def __aenter__(self) -> _PausingSession:
        return _PausingSession(await self._inner.__aenter__(), self._hook)

    async def __aexit__(self, *exc: object) -> Any:
        return await self._inner.__aexit__(*exc)


@pytest_asyncio.fixture
async def scratch_intent_db(db_env):
    """The real ``IntentDB`` bound to a throwaway database on the test PG."""
    from app.database.connection import close_db, init_db
    from app.database.intents import intent_db
    from app.database.tables import Base, IntentRow

    admin = psycopg2.connect(f"{db_env['pg_base']}/test")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')
            cur.execute(f'CREATE DATABASE "{_SCRATCH_DB}"')
    finally:
        admin.close()

    engine = await init_db(f"{db_env['pg_base']}/{_SCRATCH_DB}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[IntentRow.__table__])
    try:
        yield intent_db
    finally:
        await close_db()


async def test_status_write_cannot_erase_a_concurrent_edit(
    scratch_intent_db, monkeypatch
):
    """A tick that observed the intent pre-edit must not drop the edit.

    The interleaving is the one a 0.5s reconcile interval hits whenever a PUT
    lands mid-pass: the status write reads the row, the edit commits, the
    status write commits. Its status document was built before the edit
    existed and carries ``spec_changed_at: None``.
    """
    db = scratch_intent_db
    created = await db.create_intent(
        alias=f"race-{uuid.uuid4().hex[:8]}",
        model_source="repo://test-model:v1",
        replicas=1,
        priority="production",
        strategy="rolling",
        backend=dict(_BACKEND),
        placement={},
        resources={},
        metadata={},
    )

    read_done = asyncio.Event()
    resume = asyncio.Event()

    async def pause() -> None:
        read_done.set()
        await resume.wait()

    armed: dict[str, Any] = {"hook": None}
    real_session = type(db)._session

    def _session(self: Any) -> Any:
        inner = real_session(self)
        hook, armed["hook"] = armed["hook"], None
        return inner if hook is None else _PausingSessionCM(inner, hook)

    monkeypatch.setattr(type(db), "_session", _session)

    armed["hook"] = pause
    status_write = asyncio.create_task(
        db.update_status(
            created.id,
            phase="ready",
            reconcile="succeeded",
            status_json={
                "spec_changed_at": None,
                "strategy_progress": None,
                "replica_set": [],
            },
            # What this pass reconciled against: it loaded the intent before
            # the edit, so it saw no pending spec change.
            spec_version_seen=None,
        )
    )
    await asyncio.wait_for(read_done.wait(), timeout=30)

    edit = asyncio.create_task(
        db.update_intent(
            created.id,
            model_source=created.model_source,
            replicas=created.replicas,
            priority=created.priority,
            strategy=created.strategy,
            backend={**_BACKEND, "max_length": 256},
            placement={},
            resources={},
            metadata={},
        )
    )
    # Long enough for the edit to commit if nothing serializes it against the
    # in-flight status write, and to be blocked on the row if something does.
    await asyncio.sleep(0.5)
    resume.set()
    await asyncio.wait_for(asyncio.gather(status_write, edit), timeout=30)

    final = await db.get_intent(created.id)
    assert final is not None
    assert final.backend["max_length"] == 256, "the edit itself was lost"
    assert final.status.spec_changed_at is not None, (
        "the status write erased the marker of an edit that committed while it "
        "was in flight; the reconciler will never compare the replicas against "
        f"the new spec and the edit is dropped. intent={final.model_dump()}"
    )
