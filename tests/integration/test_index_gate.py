"""WP-15C acceptance 2 (C-1): a definitions directory that fails `validate` makes `makna index
build` and the stack's `init` step refuse, and index nothing.

`makna validate` is the gate until the lifecycle of WP-18 exists, so the build runs the same
checks, the example requests (V19) included.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, func, select

from deploy import init
from deploy.init import Context
from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.cli.main import main
from makna.index.tables import search_document, semantic_object

from ..catalog_support import DEFINITIONS, EXPORT, TAG_MAP, sample_objects

METRICS = Path("lending") / "metrics"


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


def counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        return (
            connection.execute(select(func.count()).select_from(search_document)).scalar_one(),
            connection.execute(select(func.count()).select_from(semantic_object)).scalar_one(),
        )


def copy_definitions(tmp_path: Path) -> Path:
    target = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, target)
    return target


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# One break for a rule of each kind: a value that is not one of the declared values (V10), an
# expression the dialect check refuses (V05), a relationship that joins a non-key (V21), and an
# example request that cannot compile (V19, which needs the compiler).
def break_a_value_filter(root: Path) -> str:
    edit(
        root / METRICS / "active_contract_count.yaml",
        "join_paths:",
        "filters:\n  - {dimension: contract.contract_status, op: '=', value: actve}\njoin_paths:",
    )
    return "V10"


def break_an_expression(root: Path) -> str:
    edit(
        root / "lending" / "entities" / "contract.yaml",
        "disbursement_date IS NOT NULL",
        "disbursement_date ^ 2",
    )
    return "V05"


def break_a_relationship_key(root: Path) -> str:
    edit(
        root / "lending" / "entities" / "payment.yaml",
        "keys: [{from: contract_id, to: contract_id}]",
        "keys: [{from: contract_id, to: tenor_months}]",
    )
    return "V21"


def break_an_example_request(root: Path) -> str:
    edit(
        root / METRICS / "disbursement_amount.yaml",
        "example_requests:",
        'example_requests:\n  - question: "Disbursed by a dimension that is not there"\n'
        "    request: {dimensions: [payment.no_such_dimension]}",
    )
    return "V19"


@pytest.mark.parametrize(
    "break_it",
    [break_a_value_filter, break_an_expression, break_a_relationship_key, break_an_example_request],
)
def test_a_directory_that_fails_validate_is_not_indexed(
    database: Engine,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    break_it: object,
) -> None:
    root = copy_definitions(tmp_path)
    code = break_it(root)  # type: ignore[operator]
    assert main(["validate", str(root)]) == 1
    capsys.readouterr()
    assert main(["index", "build", "--definitions", str(root)]) == 1
    err = capsys.readouterr().err
    assert f" {code} " in err  # the rule, with the file and the message before it
    assert ".yaml" in err and "not indexing" in err
    assert counts(database) == (0, 0)


def test_a_broken_file_does_not_change_an_index_that_was_built_from_good_definitions(
    database: Engine, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["index", "build", "--definitions", str(DEFINITIONS)]) == 0
    capsys.readouterr()
    built = counts(database)
    assert built[1] > 0
    root = copy_definitions(tmp_path)
    break_an_expression(root)
    assert main(["index", "build", "--definitions", str(root)]) == 1
    capsys.readouterr()
    assert counts(database) == built


def test_the_clean_sample_definitions_still_index(
    database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", str(DEFINITIONS)]) == 0
    assert main(["index", "build", "--definitions", str(DEFINITIONS)]) == 0
    capsys.readouterr()


def context(root: Path, url: URL) -> Context:
    return Context(
        data_dir=root,
        seed=1,
        scale=0.05,
        database_url=url.render_as_string(hide_password=False),
        lending_loader_url="postgresql://unused",
        definitions_dir=root,
    )


@pytest.mark.parametrize("break_it", [break_an_expression, break_an_example_request])
def test_the_stack_init_step_fails_and_indexes_nothing(
    database: Engine, migrated_url: URL, tmp_path: Path, break_it: object
) -> None:
    root = copy_definitions(tmp_path)
    code = break_it(root)  # type: ignore[operator]
    with pytest.raises(init.InitError) as caught:
        init.build_index_step(context(root, migrated_url))
    assert f" {code} " in str(caught.value) and "nothing was indexed" in str(caught.value)
    assert counts(database) == (0, 0)


def test_the_init_step_runs_all_steps_through_run_steps_and_stops_at_the_failure(
    database: Engine, migrated_url: URL, tmp_path: Path
) -> None:
    import io

    root = copy_definitions(tmp_path)
    break_an_expression(root)
    out = io.StringIO()
    step = init.Step("build-index", init.build_index_step)
    later_ran: list[str] = []
    after = init.Step("demo-users", lambda ctx: later_ran.append("ran") or "ok")
    assert init.run_steps([step, after], context(root, migrated_url), out) == 1
    assert "build-index: FAILED" in out.getvalue() and not later_ran
