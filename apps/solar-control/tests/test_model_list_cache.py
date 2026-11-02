"""Tests for the per-scope TTL cache of the aggregated models payload."""

from app.services.model_list_cache import ModelListCache


def test_get_returns_none_on_miss():
    cache = ModelListCache(ttl_s=60)
    assert cache.get(("wind",)) is None


def test_set_then_get_returns_payload():
    cache = ModelListCache(ttl_s=60)
    cache.set(("wind",), {"models": [1]})
    assert cache.get(("wind",)) == {"models": [1]}


def test_scope_is_part_of_the_key():
    cache = ModelListCache(ttl_s=60)
    cache.set(("wind",), {"models": [1]})
    assert cache.get(("core",)) is None
    assert cache.get(None) is None


def test_ttl_expiry_returns_none(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        "app.services.model_list_cache.time.monotonic", lambda: clock[0]
    )
    cache = ModelListCache(ttl_s=5)
    cache.set(None, {"models": []})
    assert cache.get(None) == {"models": []}
    clock[0] += 6
    assert cache.get(None) is None


def test_invalidate_clears_every_scope():
    cache = ModelListCache(ttl_s=60)
    cache.set(None, {"models": []})
    cache.set(("wind",), {"models": [1]})
    cache.invalidate()
    assert cache.get(None) is None
    assert cache.get(("wind",)) is None


def test_set_with_a_stale_generation_is_dropped():
    cache = ModelListCache(ttl_s=60)
    cache.set(None, {"models": []})
    stale = cache.generation
    cache.invalidate()
    cache.set(None, {"models": [1]}, generation=stale)
    assert cache.get(None) is None


def test_set_with_the_current_generation_is_stored():
    cache = ModelListCache(ttl_s=60)
    cache.set(None, {"models": []}, generation=cache.generation)
    assert cache.get(None) == {"models": []}
