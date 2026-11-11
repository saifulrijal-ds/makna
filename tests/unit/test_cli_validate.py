"""WP-03 criterion 4: `makna validate`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from makna.cli.main import main

from . import definition_fixtures as fx

EXAMPLES = Path(__file__).resolve().parents[2] / "semantic" / "schema" / "examples"


def test_validate_passes_on_the_examples(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(EXAMPLES)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_validate_prints_the_rule_code_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fx.fixture_dir(fx.v06_time_dimension_does_not_exist, tmp_path)
    assert main(["validate", str(root)]) == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    match = re.fullmatch(r"(?P<path>.+/shop/entities/order\.yaml):(?P<line>\d+): V06 .+", lines[0])
    assert match, lines[0]
    source = Path(match["path"]).read_text("utf-8").splitlines()
    assert "time_dimension: shipped_date" in source[int(match["line"]) - 1]
    assert "1 problem" in captured.err


def test_validate_prints_one_line_per_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fx.fixture_dir(fx.v13_approved_metric_depends_on_draft_metric, tmp_path)
    assert main(["validate", str(root)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert all(" V13 " in line for line in lines)


def test_validate_on_something_that_is_not_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", str(tmp_path / "missing")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_validate_needs_a_directory_argument() -> None:
    with pytest.raises(SystemExit) as info:
        main(["validate"])
    assert info.value.code == 2
