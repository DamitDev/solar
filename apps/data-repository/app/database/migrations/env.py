from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

import os
import sys

from app.database.models import Base

from dotenv import load_dotenv

load_dotenv()

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url():
    """Build database URL from POSTGRES_* environment variables."""
    db = os.getenv("POSTGRES_DB", None)
    user = os.getenv("POSTGRES_USER", None)
    password = os.getenv("POSTGRES_PASSWORD", None)
    host = os.getenv("POSTGRES_HOST", None)
    port = os.getenv("POSTGRES_PORT", None)

    if all([db, user, password, host, port]):
        return f"postgresql://{user}:{password}@{host}:{port}/{db}"
    else:
        raise EnvironmentError(
            "Database configuration environment variables are not fully set. "
            "Required: POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT"
        )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine,
    so a DBAPI is not required.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with an Engine."""
    configuration = config.get_section(config.config_ini_section)
    if configuration is None:
        configuration = {}

    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
