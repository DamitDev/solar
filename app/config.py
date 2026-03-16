from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings"""

    host: str = "0.0.0.0"
    port: int = 8000

    # Management API key (for WebUI and admin operations on /api/*)
    management_api_key: str = "change-me-management"

    # Gateway routing/health tuning
    registry_refresh_interval_s: float = 2.0
    health_check_interval_s: float = 1.0
    health_ttl_s: float = 3.0
    health_cooldown_s: float = 5.0
    route_connect_timeout_s: float = 0.5
    route_max_attempts: int = 3
    route_retry_delay_s: float = 0.15

    # Health probe mode
    health_probe_use_http: bool = False
    health_probe_http_path: str = "/v1/models"

    # PostgreSQL
    database_url: str = "postgresql://solar:solar@localhost:5432/solar_gateway"

    # Redis (shared state + Socket.IO adapter)
    redis_url: str = "redis://localhost:6379/0"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
