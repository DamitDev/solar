"""C4 pull-progress cache lifecycle.

Freshness is decided against *control's* clock (the ``at`` field is control's
receive time, not the host's), a finished pull is never "still working", and
the ``solar:hosts:pulls`` hash is pruned rather than growing forever.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.redis_state.freshness import entry_age_s
from app.routes.management.pulls import list_pulls
from app.services.reconciliation import _pull_progress_state, _PullState


def _entry(phase: str = "downloading", *, age_s: float = 0.0, tz: bool = True) -> str:
    at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return json.dumps(
        {
            "at": at.isoformat() if tz else at.replace(tzinfo=None).isoformat(),
            "data": {"source_uri": "repo://iris:v1", "phase": phase},
        }
    )


class _FakeRedis:
    def __init__(self, hash_: dict[str, str]):
        self.hash = hash_
        self.deleted: list[str] = []

    async def hget(self, _key: str, field: str):
        return self.hash.get(field)

    async def hgetall(self, _key: str):
        return dict(self.hash)

    async def hkeys(self, _key: str):
        return list(self.hash)

    async def hdel(self, _key: str, *fields: str):
        for field in fields:
            self.hash.pop(field, None)
            self.deleted.append(field)
        return len(fields)


class TestEntryAge:
    def test_naive_timestamp_is_read_as_utc(self):
        """A naive stamp must not raise; aware minus naive is a TypeError."""
        age = entry_age_s(datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
        assert age is not None
        assert age < 5

    def test_garbage_is_none(self):
        assert entry_age_s("nope") is None
        assert entry_age_s(None) is None
        assert entry_age_s(12345) is None


class TestPullProgressState:
    @pytest.mark.anyio
    async def test_recent_downloading_entry_is_fresh(self):
        redis = _FakeRedis({"h1|repo://iris:v1": _entry("downloading")})
        with patch("app.services.reconciliation.redis_client", return_value=redis):
            state = await _pull_progress_state("h1", "repo://iris:v1")
        assert state is _PullState.FRESH

    @pytest.mark.anyio
    async def test_old_entry_is_stale(self):
        redis = _FakeRedis({"h1|repo://iris:v1": _entry("downloading", age_s=10_000)})
        with patch("app.services.reconciliation.redis_client", return_value=redis):
            state = await _pull_progress_state("h1", "repo://iris:v1")
        assert state is _PullState.STALE

    @pytest.mark.anyio
    @pytest.mark.parametrize("phase", ["completed", "failed"])
    async def test_terminal_entry_is_absent_not_stale(self, phase: str):
        """A finished pull leaves no progress to wait for — but it is not a
        stalled one either.

        ABSENT rather than STALE matters twice: the recoverable flag must not
        claim the host is "still downloading" after the download ended, and the
        wait must not abandon the create/start that follows the pull.
        """
        redis = _FakeRedis({"h1|repo://iris:v1": _entry(phase)})
        with patch("app.services.reconciliation.redis_client", return_value=redis):
            state = await _pull_progress_state("h1", "repo://iris:v1")
        assert state is _PullState.ABSENT

    @pytest.mark.anyio
    async def test_no_entry_is_absent(self):
        """A cached model never emits progress, so no entry is ever written."""
        redis = _FakeRedis({})
        with patch("app.services.reconciliation.redis_client", return_value=redis):
            state = await _pull_progress_state("h1", "repo://iris:v1")
        assert state is _PullState.ABSENT

    @pytest.mark.anyio
    async def test_unreadable_entry_is_absent(self):
        """An unparseable cache is no evidence that a pull wedged."""
        redis = _FakeRedis({"h1|repo://iris:v1": "{not json"})
        with patch("app.services.reconciliation.redis_client", return_value=redis):
            state = await _pull_progress_state("h1", "repo://iris:v1")
        assert state is _PullState.ABSENT

    @pytest.mark.anyio
    async def test_naive_entry_does_not_raise(self):
        redis = _FakeRedis({"h1|repo://iris:v1": _entry("downloading", tz=False)})
        with patch("app.services.reconciliation.redis_client", return_value=redis):
            state = await _pull_progress_state("h1", "repo://iris:v1")
        assert state is _PullState.FRESH

    @pytest.mark.anyio
    async def test_missing_host_or_uri_is_absent(self):
        assert await _pull_progress_state(None, "repo://iris:v1") is _PullState.ABSENT
        assert await _pull_progress_state("h1", "") is _PullState.ABSENT


class TestListPullsPruning:
    @pytest.mark.anyio
    async def test_finished_pull_is_pruned_after_the_grace(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.pull_progress_terminal_grace_s", 60.0)
        monkeypatch.setattr("app.config.settings.pull_progress_stale_after_s", 180.0)
        redis = _FakeRedis(
            {
                "h1|repo://old:v1": _entry("completed", age_s=600),
                "h1|repo://recent:v1": _entry("completed", age_s=5),
                "h1|repo://live:v1": _entry("downloading", age_s=5),
            }
        )
        with patch("app.routes.management.pulls.redis_client", return_value=redis):
            result = await list_pulls()

        assert "h1|repo://old:v1" not in result
        assert "h1|repo://old:v1" in redis.deleted
        # Inside the grace, so a late-joining client still sees the outcome.
        assert "h1|repo://recent:v1" in result
        # A pull that is still reporting is untouched.
        assert "h1|repo://live:v1" in result

    @pytest.mark.anyio
    async def test_abandoned_download_is_pruned(self, monkeypatch):
        """A host that dies mid-pull must not be served as live progress.

        Nothing else reclaims the entry: the hash has no TTL, the host will
        never emit a terminal event for a pull it did not finish, and the
        webui would render the frozen byte count forever.
        """
        monkeypatch.setattr("app.config.settings.pull_progress_stale_after_s", 180.0)
        redis = _FakeRedis(
            {
                "h1|repo://abandoned:v1": _entry("downloading", age_s=3_600),
                # Silent for longer than one emission but inside the margin,
                # so a host that merely skipped a few updates keeps its bar.
                "h1|repo://slow:v1": _entry("downloading", age_s=200),
            }
        )
        with patch("app.routes.management.pulls.redis_client", return_value=redis):
            result = await list_pulls()

        assert "h1|repo://abandoned:v1" not in result
        assert "h1|repo://abandoned:v1" in redis.deleted
        assert "h1|repo://slow:v1" in result

    @pytest.mark.anyio
    async def test_unparseable_entry_is_dropped(self):
        redis = _FakeRedis({"h1|repo://bad:v1": "{not json"})
        with patch("app.routes.management.pulls.redis_client", return_value=redis):
            result = await list_pulls()
        assert result == {}
        assert "h1|repo://bad:v1" in redis.deleted

    @pytest.mark.anyio
    async def test_undateable_entry_is_kept(self):
        """An unusable ``at`` is not evidence of age, so the row survives."""
        redis = _FakeRedis(
            {"h1|repo://a:v1": json.dumps({"data": {"phase": "downloading"}})}
        )
        with patch("app.routes.management.pulls.redis_client", return_value=redis):
            result = await list_pulls()
        assert "h1|repo://a:v1" in result


class TestHostStatePurge:
    @pytest.mark.anyio
    async def test_removing_a_host_drops_only_its_pull_entries(self):
        from app.redis_state.hosts import HostConnectionStore

        redis = _FakeRedis(
            {
                "h1|repo://a:v1": _entry(),
                "h1|repo://b:v1": _entry(),
                "h2|repo://a:v1": _entry(),
            }
        )
        store = HostConnectionStore()
        with patch("app.redis_state.hosts.redis_client", return_value=redis):
            removed = await store.remove_host_pulls("h1")

        assert removed == 2
        assert set(redis.hash) == {"h2|repo://a:v1"}

    @pytest.mark.anyio
    async def test_purge_host_state_clears_every_keyed_map(self):
        from app.redis_state.hosts import HostConnectionStore

        store = HostConnectionStore()
        with (
            patch.object(store, "remove_host_instances", new=AsyncMock()) as instances,
            patch.object(
                store, "remove_host_resource_snapshot", new=AsyncMock()
            ) as snapshot,
            patch.object(store, "remove_host_pulls", new=AsyncMock()) as pulls,
        ):
            await store.purge_host_state("h1")

        instances.assert_awaited_once_with("h1")
        snapshot.assert_awaited_once_with("h1")
        pulls.assert_awaited_once_with("h1")

    @pytest.mark.anyio
    async def test_disconnect_drops_pulls_but_keeps_the_snapshot(self):
        """Progress can only arrive over the socket that just went away.

        The snapshot and instance cache survive: they still describe the host
        and both are freshness-gated on read, whereas a frozen ``downloading``
        row would be served as live progress until the host returns.
        """
        from app.socketio_app import host_handlers

        with (
            patch.object(
                host_handlers.host_store,
                "unregister_host_by_sid",
                new=AsyncMock(return_value="h1"),
            ),
            patch.object(
                host_handlers.host_store, "set_disconnect_time", new=AsyncMock()
            ),
            patch.object(
                host_handlers.host_store, "remove_host_pulls", new=AsyncMock()
            ) as pulls,
            patch.object(
                host_handlers.host_store,
                "remove_host_resource_snapshot",
                new=AsyncMock(),
            ) as snapshot,
            patch.object(host_handlers.host_db, "update_host_status", new=AsyncMock()),
            patch.object(
                host_handlers.host_db, "get_host", new=AsyncMock(return_value=None)
            ),
        ):
            await host_handlers.host_disconnect("sid-1")

        pulls.assert_awaited_once_with("h1")
        snapshot.assert_not_awaited()
