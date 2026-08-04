"""Minimal import checks so CI pytest has a non-empty suite."""


def test_settings_load() -> None:
    from app.config import settings

    assert settings.host == "0.0.0.0"
    assert isinstance(settings.port, int) and settings.port > 0
    assert settings.database_url.startswith("postgresql://")
    assert settings.redis_url.startswith("redis://")
