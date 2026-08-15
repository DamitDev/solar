"""Split API keys from endpoints into their own table (S-045)

An endpoint used to *be* an API key: api_endpoints.api_key was unique and
auth resolved straight off it. This migration breaks that 1:1 coupling:

- creates ``api_keys`` (many keys per endpoint, endpoint_id NOT NULL with
  CASCADE delete)
- backfills one ``default`` key per endpoint from the old ``api_key`` column
- adds ``serve_all_models`` / ``model_patterns`` to ``api_endpoints``
- drops ``api_endpoints.api_key``

``downgrade()`` is reversible: it re-creates ``api_key``, backfills from the
oldest (earliest-created) key per endpoint, then drops ``api_keys`` and the
scoping columns.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(conn, table: str) -> bool:
    return sa.inspect(conn).has_table(table)


def _has_column(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_name = :table AND column_name = :column"
            ")"
        ),
        {"table": table, "column": column},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, "api_keys"):
        op.create_table(
            "api_keys",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=False),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "endpoint_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("api_endpoints.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("key", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index("ix_api_keys_endpoint_id", "api_keys", ["endpoint_id"])
        op.create_index("ix_api_keys_key", "api_keys", ["key"], unique=True)
        op.create_index(
            "uq_api_keys_endpoint_name",
            "api_keys",
            ["endpoint_id", "name"],
            unique=True,
        )

    # One default key per endpoint, preserving the credential already in use.
    conn.execute(
        sa.text(
            "INSERT INTO api_keys (endpoint_id, name, key)"
            " SELECT id, 'default', api_key FROM api_endpoints"
            " WHERE api_key IS NOT NULL"
            " ON CONFLICT DO NOTHING"
        )
    )

    if not _has_column(conn, "api_endpoints", "serve_all_models"):
        op.add_column(
            "api_endpoints",
            sa.Column(
                "serve_all_models",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )
    if not _has_column(conn, "api_endpoints", "model_patterns"):
        op.add_column(
            "api_endpoints",
            sa.Column(
                "model_patterns",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )

    if _has_column(conn, "api_endpoints", "api_key"):
        op.drop_column("api_endpoints", "api_key")


def downgrade() -> None:
    conn = op.get_bind()

    if _has_table(conn, "api_keys") and not _has_column(
        conn, "api_endpoints", "api_key"
    ):
        # Backfill from the oldest key per endpoint (deterministic: first
        # created, then lexicographically smallest key as a tie-breaker).
        op.add_column("api_endpoints", sa.Column("api_key", sa.Text(), nullable=True))
        conn.execute(
            sa.text(
                "UPDATE api_endpoints ep"
                " SET api_key = k.key"
                " FROM ("
                "   SELECT DISTINCT ON (endpoint_id) endpoint_id, key"
                "   FROM api_keys"
                "   ORDER BY endpoint_id, created_at ASC, key ASC"
                " ) k"
                " WHERE k.endpoint_id = ep.id"
            )
        )
        null_count = conn.execute(
            sa.text("SELECT count(*) FROM api_endpoints WHERE api_key IS NULL")
        ).scalar()
        if null_count == 0:
            op.alter_column("api_endpoints", "api_key", nullable=False)
        op.create_index(
            "ix_api_endpoints_api_key", "api_endpoints", ["api_key"], unique=True
        )

    if _has_column(conn, "api_endpoints", "serve_all_models"):
        op.drop_column("api_endpoints", "serve_all_models")
    if _has_column(conn, "api_endpoints", "model_patterns"):
        op.drop_column("api_endpoints", "model_patterns")

    if _has_table(conn, "api_keys"):
        op.drop_index("uq_api_keys_endpoint_name", table_name="api_keys")
        op.drop_index("ix_api_keys_key", table_name="api_keys")
        op.drop_index("ix_api_keys_endpoint_id", table_name="api_keys")
        op.drop_table("api_keys")
