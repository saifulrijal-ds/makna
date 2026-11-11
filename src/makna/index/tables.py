"""The tables of ARCHITECTURE section 6.4, as SQLAlchemy Core metadata.

The Alembic migrations in `makna.index.migrations` create these tables; a test compares the
two. The tables fall in three groups: the catalog copy (written by the sync job), the semantic
index (written by the indexer), and operational data (not rebuildable).
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Column,
    Computed,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, TSVECTOR

metadata = MetaData()


def _id() -> Column[int]:
    return Column("id", BigInteger, Identity(), primary_key=True)


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> Column[object]:
    return Column(
        name,
        TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


def _fk(name: str, target: str, *, nullable: bool = False) -> Column[int]:
    return Column(name, BigInteger, ForeignKey(target, ondelete="CASCADE"), nullable=nullable)


# --- Catalog copy (written only by the sync job) ---------------------------------------------

catalog_source = Table(
    "catalog_source",
    metadata,
    _id(),
    Column("name", Text, nullable=False, unique=True),
    Column("connector", Text, nullable=False),
    _ts("last_sync_at"),
)

catalog_dataset = Table(
    "catalog_dataset",
    metadata,
    _id(),
    _fk("source_id", "catalog_source.id"),
    Column("engine_fqn", Text, nullable=False, unique=True),
    Column("catalog_id", Text),
    Column("name", Text, nullable=False),
    Column("description", Text),
    Column("owners", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("tags", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("content_hash", Text, nullable=False),
    _ts("synced_at", nullable=False),
    _ts("removed_at"),
)

catalog_column = Table(
    "catalog_column",
    metadata,
    _id(),
    _fk("dataset_id", "catalog_dataset.id"),
    Column("name", Text, nullable=False),
    Column("data_type", Text, nullable=False),
    Column("native_type", Text),
    Column("description", Text),
    Column("tags", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("nullable", Boolean, nullable=False, server_default=text("true")),
    Column("is_primary_key", Boolean, nullable=False, server_default=text("false")),
    Column("ordinal", Integer, nullable=False),
    Column("derived_sensitivity", Text),
    UniqueConstraint("dataset_id", "name", name="uq_catalog_column_dataset_name"),
)

catalog_glossary_term = Table(
    "catalog_glossary_term",
    metadata,
    _id(),
    _fk("source_id", "catalog_source.id"),
    Column("catalog_id", Text),
    Column("name", Text, nullable=False),
    Column("definition", Text),
    Column("synonyms", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
)

sync_run = Table(
    "sync_run",
    metadata,
    _id(),
    _fk("source_id", "catalog_source.id"),
    _ts("started_at", nullable=False),
    _ts("finished_at"),
    Column("status", Text, nullable=False),
    Column("summary", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
)

# --- Semantic index (written only by the indexer) --------------------------------------------
# No foreign keys in this group: the sync job writes `reference_issue` before the indexer has
# written `semantic_object`, and `search_document` also holds catalog columns and glossary terms.

semantic_object = Table(
    "semantic_object",
    metadata,
    Column("id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("domain", Text),
    Column("label", Text),
    Column("status", Text, nullable=False),
    Column("has_proposal", Boolean, nullable=False, server_default=text("false")),
    Column("proposal_status", Text),
    Column("official", JSONB),
    Column("proposal", JSONB),
    Column("official_path", Text),
    Column("official_commit", Text),
    Column("proposal_commit", Text),
    Column("version", Integer),
    Column("owner_team", Text),
    Column("sensitivity", Text),
    Column("derived", JSONB),
    _ts("updated_at", nullable=False, default_now=True),
)

object_edge = Table(
    "object_edge",
    metadata,
    Column("src_id", Text, primary_key=True),
    Column("dst_id", Text, primary_key=True),
    Column("type", Text, primary_key=True),
    Index("ix_object_edge_dst_id", "dst_id"),
)

reference_issue = Table(
    "reference_issue",
    metadata,
    _id(),
    Column("object_id", Text, nullable=False),
    Column("part", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("message", Text, nullable=False),
    _ts("opened_at", nullable=False, default_now=True),
    _ts("closed_at"),
    Index("ix_reference_issue_object_id", "object_id"),
)

search_document = Table(
    "search_document",
    metadata,
    _id(),
    Column("object_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("domain", Text),
    Column("names", Text, nullable=False, server_default=""),
    Column("description", Text, nullable=False, server_default=""),
    Column("extra", Text, nullable=False, server_default=""),
    # `simple` configuration: no stemming, which suits mixed Indonesian and English (section 8.2).
    Column(
        "tsv",
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple', names), 'A') || "
            "setweight(to_tsvector('simple', description), 'B') || "
            "setweight(to_tsvector('simple', extra), 'C')",
            persisted=True,
        ),
    ),
    Column("embedding", Vector()),  # no fixed size; exact search, no vector index (section 8.1)
    Column("text_hash", Text, nullable=False),
    Index("ix_search_document_object_id", "object_id"),
    Index("ix_search_document_tsv", "tsv", postgresql_using="gin"),
)

index_state = Table(
    "index_state",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

data_freshness = Table(
    "data_freshness",
    metadata,
    Column("object_id", Text, primary_key=True),
    Column("latest_value", Date),
    _ts("checked_at", nullable=False),
)

# --- Operational (not rebuildable; back these up) --------------------------------------------

app_user = Table(
    "app_user",
    metadata,
    _id(),
    Column("email", Text, nullable=False, unique=True),
    Column("display_name", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("teams", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    _ts("created_at", nullable=False, default_now=True),
)

api_token = Table(
    "api_token",
    metadata,
    _id(),
    Column("user_id", BigInteger, ForeignKey("app_user.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("token_hash", Text, nullable=False, unique=True),
    _ts("created_at", nullable=False, default_now=True),
    _ts("last_used_at"),
    _ts("revoked_at"),
)

audit_event = Table(
    "audit_event",
    metadata,
    _id(),
    _ts("at", nullable=False, default_now=True),
    Column("actor_user_id", BigInteger, ForeignKey("app_user.id")),
    Column("actor_label", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("object_id", Text, nullable=False),
    Column("from_status", Text),
    Column("to_status", Text),
    Column("reason", Text),
    Column("commit_sha", Text),
    UniqueConstraint(
        "commit_sha", "object_id", "action", name="uq_audit_event_commit_object_action"
    ),
)

tool_call_log = Table(
    "tool_call_log",
    metadata,
    _id(),
    _ts("at", nullable=False, default_now=True),
    Column("channel", Text, nullable=False),
    Column("user_id", BigInteger, ForeignKey("app_user.id")),
    Column("token_id", BigInteger, ForeignKey("api_token.id")),
    Column("tool", Text, nullable=False),
    Column("request", JSONB),
    Column("result", JSONB),
    Column("status", Text, nullable=False),
    Column("error_code", Text),
    Column("duration_ms", Integer),
    Index("ix_tool_call_log_at", "at"),
)

TABLE_NAMES = tuple(sorted(metadata.tables))
