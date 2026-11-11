"""WP-05 criterion 3: Alembic creates every table of ARCHITECTURE section 6.4 and removes them."""

from __future__ import annotations

from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import URL

from makna.index import migrate
from makna.index.tables import metadata

# Section 6.4, table by table.
SECTION_6_4_COLUMNS = {
    "catalog_source": ["id", "name", "connector", "last_sync_at"],
    "catalog_dataset": [
        "id", "source_id", "engine_fqn", "catalog_id", "name", "description", "owners", "tags",
        "content_hash", "synced_at", "removed_at",
    ],
    "catalog_column": [
        "id", "dataset_id", "name", "data_type", "native_type", "description", "tags", "nullable",
        "is_primary_key", "ordinal", "derived_sensitivity",
    ],
    "catalog_glossary_term": ["id", "source_id", "catalog_id", "name", "definition", "synonyms"],
    "sync_run": ["id", "source_id", "started_at", "finished_at", "status", "summary"],
    "semantic_object": [
        "id", "kind", "name", "domain", "label", "status", "has_proposal", "proposal_status",
        "official", "proposal", "official_path", "official_commit", "proposal_commit", "version",
        "owner_team", "sensitivity", "derived", "updated_at",
    ],
    "object_edge": ["src_id", "dst_id", "type"],
    "reference_issue": ["id", "object_id", "part", "code", "message", "opened_at", "closed_at"],
    "search_document": [
        "id", "object_id", "kind", "status", "domain", "names", "description", "extra", "tsv",
        "embedding", "text_hash",
    ],
    "index_state": ["key", "value"],
    "data_freshness": ["object_id", "latest_value", "checked_at"],
    "app_user": [
        "id", "email", "display_name", "password_hash", "role", "teams", "is_active", "created_at",
    ],
    "api_token": [
        "id", "user_id", "name", "token_hash", "created_at", "last_used_at", "revoked_at",
    ],
    "audit_event": [
        "id", "at", "actor_user_id", "actor_label", "action", "object_id", "from_status",
        "to_status", "reason", "commit_sha",
    ],
    "tool_call_log": [
        "id", "at", "channel", "user_id", "token_id", "tool", "request", "result", "status",
        "error_code", "duration_ms",
    ],
}  # fmt: skip


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_upgrade_creates_every_table_of_section_6_4(empty_database: URL, engine: Engine) -> None:
    assert _tables(engine) == set()
    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    assert _tables(engine) == set(SECTION_6_4_COLUMNS)
    inspector = inspect(engine)
    for table, columns in SECTION_6_4_COLUMNS.items():
        assert [c["name"] for c in inspector.get_columns(table)] == columns, table


def test_vector_extension_is_present_after_upgrade(empty_database: URL, engine: Engine) -> None:
    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
    assert found == "vector"


def test_downgrade_to_base_removes_every_table(empty_database: URL, engine: Engine) -> None:
    url = empty_database.render_as_string(hide_password=False)
    migrate.upgrade(url)
    migrate.downgrade(url)
    assert _tables(engine) == set()
    migrate.upgrade(url)  # and the cycle can run again
    assert _tables(engine) == set(SECTION_6_4_COLUMNS)


def test_upgrade_twice_changes_nothing(empty_database: URL, engine: Engine) -> None:
    url = empty_database.render_as_string(hide_password=False)
    migrate.upgrade(url)
    migrate.upgrade(url)
    assert _tables(engine) == set(SECTION_6_4_COLUMNS)


def test_core_metadata_matches_the_migrated_database(empty_database: URL, engine: Engine) -> None:
    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    inspector = inspect(engine)
    assert set(metadata.tables) == set(SECTION_6_4_COLUMNS)
    for name, table in metadata.tables.items():
        live = {c["name"]: c for c in inspector.get_columns(name)}
        assert list(live) == [c.name for c in table.columns], name
        for column in table.columns:
            if not column.primary_key:
                assert live[column.name]["nullable"] == column.nullable, (name, column.name)


def test_search_document_generates_tsv_and_stores_a_vector(
    empty_database: URL, engine: Engine
) -> None:
    from sqlalchemy import insert, select

    from makna.index.tables import search_document

    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    with engine.begin() as connection:
        connection.execute(
            insert(search_document).values(
                object_id="metric:npf_ratio",
                kind="metric",
                status="approved",
                names="npf ratio kredit macet",
                description="Share of financing that is non-performing",
                embedding=[0.1, 0.2, 0.3],
                text_hash="h",
            )
        )
        match = connection.execute(
            select(search_document.c.object_id).where(
                search_document.c.tsv.op("@@")(text("to_tsquery('simple', 'macet')"))
            )
        ).scalar()
        dimension = connection.execute(
            text("SELECT vector_dims(embedding) FROM search_document")
        ).scalar()
    assert match == "metric:npf_ratio"
    assert dimension == 3
