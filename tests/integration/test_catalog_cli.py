"""WP-06: `makna catalog sync` and `makna catalog issues` against a scratch database."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, func, select

from makna.cli.main import main
from makna.index.tables import catalog_dataset, sync_run

from ..catalog_support import DEFINITIONS, EXPORT, export_without_column


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def database(migrated_url: URL, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("MAKNA_DATABASE_URL", migrated_url.render_as_string(hide_password=False))
    return migrated_engine


def test_sync_prints_a_summary_and_issues_lists_nothing_for_the_sample(
    database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(
        capsys, "catalog", "sync", "--file", str(EXPORT), "--definitions", str(DEFINITIONS)
    )
    assert code == 0, err
    assert "datasets: added 9" in out and "reference_issues: opened 0, closed 0" in out
    with database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(catalog_dataset)).scalar() == 9
    assert run(capsys, "catalog", "issues") == (0, "", "")
    code, out, _ = run(
        capsys, "catalog", "sync", "--file", str(EXPORT), "--definitions", str(DEFINITIONS)
    )
    assert code == 0 and "added 0, changed 0, removed 0, unchanged 9" in out
    with database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(sync_run)).scalar() == 2


def test_issues_lists_one_line_per_open_issue(
    database: Engine, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    reduced = export_without_column(tmp_path / "r.yaml", "customers", "city")
    code, _, err = run(
        capsys, "catalog", "sync", "--file", str(reduced), "--definitions", str(DEFINITIONS)
    )
    assert code == 0, err
    code, out, _ = run(capsys, "catalog", "issues")
    assert code == 0
    lines = out.splitlines()
    assert lines and all(line.startswith("R02 entity:") for line in lines)
    assert any("dimension:city" in line and "city is not in table" in line for line in lines)


def test_a_bad_file_exits_1_with_the_field_on_stderr_and_writes_nothing(
    database: Engine, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        EXPORT.read_text(encoding="utf-8").replace("data_type: integer", "data_type: int", 1),
        encoding="utf-8",
    )
    code, out, err = run(capsys, "catalog", "sync", "--file", str(bad))
    assert code == 1 and out == ""
    assert "makna catalog sync:" in err and "data_type" in err
    with database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(catalog_dataset)).scalar() == 0


def test_a_missing_file_and_a_missing_database_url_exit_non_zero(
    database: Engine,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, _, err = run(capsys, "catalog", "sync", "--file", str(tmp_path / "none.yaml"))
    assert code == 1 and "cannot read file" in err
    monkeypatch.delenv("MAKNA_DATABASE_URL")
    code, _, err = run(capsys, "catalog", "issues")
    assert code == 2 and "MAKNA_DATABASE_URL" in err
    code, _, err = run(capsys, "catalog", "sync", "--file", str(EXPORT))
    assert code == 2 and "MAKNA_DATABASE_URL" in err
