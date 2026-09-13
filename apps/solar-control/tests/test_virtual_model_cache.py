"""Unit tests for the virtual model TTL cache (S-060)."""

from app.models.virtual_model import VirtualModelContract, VirtualModelResponse
from app.services.virtual_model_cache import VirtualModelCache


def _entry(name="team-chat"):
    return VirtualModelResponse(
        id="00000000-0000-0000-0000-000000000000",
        name=name,
        targets=["a:8b"],
        contract=VirtualModelContract(),
    )


class TestVirtualModelCache:
    def test_miss_then_hit(self):
        cache = VirtualModelCache()
        assert cache.get_all() is None
        cache.set_all([_entry()])
        assert cache.get_all() == [_entry()]

    def test_invalidate_forces_miss(self):
        cache = VirtualModelCache()
        cache.set_all([_entry()])
        cache.invalidate()
        assert cache.get_all() is None

    def test_expiry(self):
        cache = VirtualModelCache(ttl_s=0.0)
        cache.set_all([_entry()])
        assert cache.get_all() is None
