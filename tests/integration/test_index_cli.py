"""WP-07: `makna index build` and `makna search` against a scratch database (hash provider)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, func, select

from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.cli.main import main
from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer
from makna.index.tables import search_document
from makna.search.embedding import HashProvider

from ..catalog_support import DEFINITIONS, EXPORT, TAG_MAP, sample_objects


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def database(migrated_url: URL, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("MAKNA_DATABASE_URL", migrated_url.render_as_string(hide_password=False))
    monkeypatch.setenv("MAKNA_EMBEDDING_PROVIDER", "hash")
    entities, terms = sample_objects()
    sync_catalog(
        migrated_engine,
        FileConnector(EXPORT),
        tag_map=TAG_MAP,
        entities=entities,
        glossary_terms=terms,
    )
    return migrated_engine


def count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(search_document)).scalar_one()


def test_build_then_search(
    database: Engine, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, err = run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    assert code == 0, err
    assert "embedding model: hash-trigram-256" in out
    assert "documents: added " in out and "removed 0, unchanged 0" in out
    assert "metric 25" in out and "column 92" in out
    built = count(database)

    code, out, err = run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    assert code == 0, err
    assert "added 0, changed 0, removed 0" in out and f"unchanged {built}" in out

    code, out, err = run(capsys, "search", "kredit macet")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("confidence: ")
    assert any(line.startswith("1. ") for line in lines)
    assert "metric:npf_ratio" in out
    assert "metric:write_off_amount" not in out


def test_search_options_reach_the_search(
    database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    code, out, _ = run(capsys, "search", "hapus buku", "--role", "curator", "--limit", "10")
    assert code == 0 and "metric:write_off_amount  [draft]" in out
    code, out, _ = run(capsys, "search", "national id", "--kind", "column", "--limit", "10")
    assert code == 0 and "customers.national_id" not in out
    code, out, _ = run(
        capsys, "search", "national id", "--kind", "column", "--role", "curator", "--limit", "10"
    )
    assert code == 0 and "customers.national_id" in out
    code, out, _ = run(
        capsys, "search", "NPL ratio retired", "--include-deprecated", "--limit", "10"
    )
    assert code == 0 and "metric:npl_ratio  [deprecated]" in out


def test_a_low_confidence_search_prints_the_guidance(
    database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    code, out, _ = run(capsys, "search", "website conversion funnel")
    assert code == 0
    assert out.startswith("confidence: low")
    assert "do not approximate it with a different metric" in out


def test_search_before_any_build_says_what_to_do(
    database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(capsys, "search", "npf")
    assert code == 1 and "makna index build" in err


def test_build_refuses_unreadable_definitions_and_leaves_the_index(
    database: Engine, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    built = count(database)
    broken = tmp_path / "defs"
    broken.mkdir()
    (broken / "bad.yaml").write_text("kind: metric\nname: [oops\n", encoding="utf-8")
    code, _, err = run(capsys, "index", "build", "--definitions", str(broken))
    assert code == 1 and "bad.yaml" in err and "not indexing" in err
    assert count(database) == built


def test_build_without_a_definitions_directory_is_a_usage_error(
    database: Engine, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, _, err = run(capsys, "index", "build", "--definitions", str(tmp_path / "missing"))
    assert code == 2 and "no definitions directory" in err


def test_build_without_a_database_url_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    code, _, err = run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    assert code == 2 and "MAKNA_DATABASE_URL" in err


def test_changing_the_provider_embeds_everything_again(
    database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    # a hash provider of another size stands in for "another model"
    objects = definition_objects(load_definitions(DEFINITIONS))
    result = indexer.build(database, HashProvider(64), objects)
    assert result.reembedded_everything
    assert result.documents_changed == result.documents_embedded == count(database)


def test_build_refuses_a_directory_with_no_definitions_and_leaves_the_index(
    database: Engine, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    built = count(database)
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _, err = run(capsys, "index", "build", "--definitions", str(empty))
    assert code == 1 and "no definition objects" in err
    assert count(database) == built


def test_the_build_stores_the_dimensions_the_policy_and_the_edges(
    database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """WP-12: `makna index build` passes the service layer's hook, with the configured policy."""
    from sqlalchemy import text

    from makna.model.governance import GovernancePolicy

    config = tmp_path / "makna.toml"
    config.write_text('[governance]\nmode = "custom"\nallow_personal_filters = true\n')
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "makna.toml").write_text(config.read_text())
    code, out, err = run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    assert code == 0, err
    assert "edges: added " in out and "R05: opened 0, closed 0" in out
    with database.connect() as connection:
        state = {k: v for k, v in connection.execute(text("SELECT key, value FROM index_state"))}
        stored = connection.execute(
            text(
                "SELECT derived -> 'dimensions' FROM semantic_object WHERE id = 'metric:npf_ratio'"
            )
        ).scalar_one()
    assert (
        state["governance_fingerprint"]
        == GovernancePolicy.custom(allow_personal_filters=True).fingerprint()
    )
    assert stored and any(d["ref"] == "branch.region_name" for d in stored)

    code, out, err = run(capsys, "index", "build", "--definitions", str(DEFINITIONS))
    assert code == 0, err
    assert "edges: added 0, removed 0" in out and "added 0, changed 0, removed 0" in out
