"""WP-18: `init` seeds the definitions repository once and indexes it as an application action."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from deploy import init
from deploy.init import Context
from makna.definitions.repo import GitRepo
from makna.index import migrate
from tests.catalog_support import DEFINITIONS

from . import lifecycle_support as sup


def tree_digest(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_init_seeds_a_repository_once_and_a_second_run_changes_nothing(
    empty_database: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAKNA_EMBEDDING_PROVIDER", "hash")
    url = empty_database.render_as_string(hide_password=False)
    migrate.upgrade(url)
    repo_path = tmp_path / "repo"
    context = Context(
        data_dir=tmp_path,
        seed=1,
        scale=0.05,
        database_url=url,
        lending_loader_url="postgresql://unused",
        catalog_file=Path("data/catalog/lending.catalog.yaml"),
        definitions_dir=DEFINITIONS,
        definitions_repo=repo_path,
    )
    before = tree_digest(DEFINITIONS)
    steps = [init.seed_definitions_step, init.sync_catalog_step, init.build_index_step]
    first = [step(context) for step in steps]
    assert first[0].startswith("seeded") and first[2].startswith("indexed from the repository")
    assert tree_digest(DEFINITIONS) == before  # the source tree is never written to
    repo = GitRepo(repo_path)
    assert len(repo.log()) == 1
    engine = create_engine(empty_database)
    try:
        rows = sup.audit_rows(engine)
        assert [(r["action"], r["object_id"], r["commit_sha"]) for r in rows] == [
            ("seed", "repository", repo.rev_parse("HEAD"))
        ]
        with engine.connect() as connection:
            official = connection.execute(
                text("SELECT count(*) FROM semantic_object WHERE official IS NOT NULL")
            ).scalar_one()
            commit = connection.execute(
                text("SELECT official_commit FROM semantic_object WHERE id = 'metric:npf_ratio'")
            ).scalar_one()
        assert official > 30 and commit == repo.rev_parse("HEAD")
        counters = sup.state_dump(engine)
        second = [step(context) for step in steps]
        assert second[0].startswith("unchanged") and second[2] == "unchanged"
        assert len(repo.log()) == 1 and sup.state_dump(engine) == counters
    finally:
        engine.dispose()


def test_a_non_empty_directory_is_left_alone(empty_database: URL, tmp_path: Path) -> None:
    url = empty_database.render_as_string(hide_password=False)
    migrate.upgrade(url)
    target = tmp_path / "repo"
    target.mkdir()
    (target / "notes.txt").write_text("mine")
    context = Context(
        data_dir=tmp_path, seed=1, scale=0.05, database_url=url,
        lending_loader_url="postgresql://unused", definitions_dir=DEFINITIONS,
        definitions_repo=target,
    )  # fmt: skip
    assert init.seed_definitions_step(context).startswith("unchanged")
    assert sorted(p.name for p in target.iterdir()) == ["notes.txt"]
