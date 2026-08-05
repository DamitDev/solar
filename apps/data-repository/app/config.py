import sys

from pydantic_settings import BaseSettings

# Under pytest, never read the developer-local .env — the test suite must
# be hermetic and unaffected by local overrides. Env vars still take
# precedence, so spawned processes are unaffected.
_TESTING = "pytest" in sys.modules


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "data_repository"
    postgres_user: str = "datarepo"
    postgres_password: str = "datarepo"

    harbor_url: str = "https://imgrepo.damit.hu"
    harbor_username: str = ""
    harbor_password: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def async_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    class Config:
        # Under pytest, never read the developer-local .env — the test
        # suite must be hermetic and unaffected by local overrides.
        env_file = None if _TESTING else ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
