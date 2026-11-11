"""All tables of ARCHITECTURE section 6.4, and the vector extension.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TIMESTAMPTZ = pg.TIMESTAMP(timezone=True)
EMPTY_ARRAY = sa.text("'[]'::jsonb")
NOW = sa.text("now()")


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True)


def _fk(column: str, target: str) -> sa.Column[int]:
    return sa.Column(
        column, sa.BigInteger, sa.ForeignKey(target, ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    # Already present when the database was prepared by a superuser (deploy/postgres-init);
    # creating it here covers a plain PostgreSQL where the migrating role may do so.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Catalog copy
    op.create_table(
        "catalog_source",
        _id(),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("connector", sa.Text, nullable=False),
        sa.Column("last_sync_at", TIMESTAMPTZ),
    )
    op.create_table(
        "catalog_dataset",
        _id(),
        _fk("source_id", "catalog_source.id"),
        sa.Column("engine_fqn", sa.Text, nullable=False, unique=True),
        sa.Column("catalog_id", sa.Text),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("owners", pg.JSONB, nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("tags", pg.JSONB, nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("synced_at", TIMESTAMPTZ, nullable=False),
        sa.Column("removed_at", TIMESTAMPTZ),
    )
    op.create_table(
        "catalog_column",
        _id(),
        _fk("dataset_id", "catalog_dataset.id"),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("data_type", sa.Text, nullable=False),
        sa.Column("native_type", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("tags", pg.JSONB, nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("nullable", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("is_primary_key", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("derived_sensitivity", sa.Text),
        sa.UniqueConstraint("dataset_id", "name", name="uq_catalog_column_dataset_name"),
    )
    op.create_table(
        "catalog_glossary_term",
        _id(),
        _fk("source_id", "catalog_source.id"),
        sa.Column("catalog_id", sa.Text),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("definition", sa.Text),
        sa.Column("synonyms", pg.JSONB, nullable=False, server_default=EMPTY_ARRAY),
    )
    op.create_table(
        "sync_run",
        _id(),
        _fk("source_id", "catalog_source.id"),
        sa.Column("started_at", TIMESTAMPTZ, nullable=False),
        sa.Column("finished_at", TIMESTAMPTZ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("summary", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )

    # Semantic index
    op.create_table(
        "semantic_object",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("domain", sa.Text),
        sa.Column("label", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("has_proposal", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("proposal_status", sa.Text),
        sa.Column("official", pg.JSONB),
        sa.Column("proposal", pg.JSONB),
        sa.Column("official_path", sa.Text),
        sa.Column("official_commit", sa.Text),
        sa.Column("proposal_commit", sa.Text),
        sa.Column("version", sa.Integer),
        sa.Column("owner_team", sa.Text),
        sa.Column("sensitivity", sa.Text),
        sa.Column("derived", pg.JSONB),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
    )
    op.create_table(
        "object_edge",
        sa.Column("src_id", sa.Text, primary_key=True),
        sa.Column("dst_id", sa.Text, primary_key=True),
        sa.Column("type", sa.Text, primary_key=True),
    )
    op.create_index("ix_object_edge_dst_id", "object_edge", ["dst_id"])
    op.create_table(
        "reference_issue",
        _id(),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("part", sa.Text, nullable=False),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("opened_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.Column("closed_at", TIMESTAMPTZ),
    )
    op.create_index("ix_reference_issue_object_id", "reference_issue", ["object_id"])
    op.create_table(
        "search_document",
        _id(),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("domain", sa.Text),
        sa.Column("names", sa.Text, nullable=False, server_default=""),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("extra", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "tsv",
            pg.TSVECTOR,
            sa.Computed(
                "setweight(to_tsvector('simple', names), 'A') || "
                "setweight(to_tsvector('simple', description), 'B') || "
                "setweight(to_tsvector('simple', extra), 'C')",
                persisted=True,
            ),
        ),
        sa.Column("embedding", Vector()),
        sa.Column("text_hash", sa.Text, nullable=False),
    )
    op.create_index("ix_search_document_object_id", "search_document", ["object_id"])
    op.create_index("ix_search_document_tsv", "search_document", ["tsv"], postgresql_using="gin")
    op.create_table(
        "index_state",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )
    op.create_table(
        "data_freshness",
        sa.Column("object_id", sa.Text, primary_key=True),
        sa.Column("latest_value", sa.Date),
        sa.Column("checked_at", TIMESTAMPTZ, nullable=False),
    )

    # Operational
    op.create_table(
        "app_user",
        _id(),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column(
            "teams", pg.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
    )
    op.create_table(
        "api_token",
        _id(),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.Column("last_used_at", TIMESTAMPTZ),
        sa.Column("revoked_at", TIMESTAMPTZ),
    )
    op.create_table(
        "audit_event",
        _id(),
        sa.Column("at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.Column("actor_user_id", sa.BigInteger, sa.ForeignKey("app_user.id")),
        sa.Column("actor_label", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("from_status", sa.Text),
        sa.Column("to_status", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("commit_sha", sa.Text),
        sa.UniqueConstraint(
            "commit_sha", "object_id", "action", name="uq_audit_event_commit_object_action"
        ),
    )
    op.create_table(
        "tool_call_log",
        _id(),
        sa.Column("at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("app_user.id")),
        sa.Column("token_id", sa.BigInteger, sa.ForeignKey("api_token.id")),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("request", pg.JSONB),
        sa.Column("result", pg.JSONB),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text),
        sa.Column("duration_ms", sa.Integer),
    )
    op.create_index("ix_tool_call_log_at", "tool_call_log", ["at"])


def downgrade() -> None:
    # The vector extension stays: it belongs to the database, not to these tables.
    for table in (
        "tool_call_log",
        "audit_event",
        "api_token",
        "app_user",
        "data_freshness",
        "index_state",
        "search_document",
        "reference_issue",
        "object_edge",
        "semantic_object",
        "sync_run",
        "catalog_glossary_term",
        "catalog_column",
        "catalog_dataset",
        "catalog_source",
    ):
        op.drop_table(table)
