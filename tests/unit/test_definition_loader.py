"""WP-03 criteria 1 and 5, and the loader's behaviour on bad input."""

from __future__ import annotations

from pathlib import Path

import pytest

from makna.definitions.loader import load_definitions, load_text
from makna.definitions.validate import validate

EXAMPLES = Path(__file__).resolve().parents[2] / "semantic" / "schema" / "examples"


def test_every_example_loads_and_validates_with_zero_errors() -> None:
    definitions = load_definitions(EXAMPLES)
    assert len(definitions.files) == 16
    assert all(f.definition is not None for f in definitions.files)
    assert definitions.load_issues == []
    assert [i.format() for i in validate(definitions)] == []


def test_examples_are_official_files() -> None:
    assert not any(f.is_proposal for f in load_definitions(EXAMPLES).files)


def test_a_yaml_tag_payload_is_a_clean_error_and_runs_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    payload = f"!!python/object/apply:os.system ['touch {marker}']\n"
    file, issues = load_text("lending/metrics/x.yaml", payload)
    assert file.definition is None
    assert [i.code for i in issues] == ["V01"]
    assert "constructor" in issues[0].message
    assert not marker.exists()


def test_a_tag_payload_inside_a_mapping_is_a_clean_error(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    text = (
        "apiVersion: makna/v1\nkind: glossary_term\n"
        f"name: !!python/object/apply:os.system ['touch {marker}']\n"
    )
    root = tmp_path / "repo"
    (root / "lending" / "glossary").mkdir(parents=True)
    (root / "lending" / "glossary" / "x.yaml").write_text(text, encoding="utf-8")
    definitions = load_definitions(root)
    assert [(i.code, i.line) for i in definitions.load_issues] == [("V01", 3)]
    assert not marker.exists()


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("a: &x 1\nb: *x\n", "aliases"),
        ("a: 1\na: 2\n", "duplicate key"),
        ("a: 1\n---\nb: 2\n", "not readable"),
        ("a: [1, 2\n", "not readable"),
        ("- 1\n- 2\n", "Input should be a valid dictionary"),
        ("", "empty"),
        ("# only a comment\n", "empty"),
        ("kind: sandwich\n", "kind"),
    ],
)
def test_bad_files_give_one_clean_v01_line_each(text: str, fragment: str) -> None:
    file, issues = load_text("lending/entities/x.yaml", text)
    assert file.definition is None
    assert issues
    assert {i.code for i in issues} == {"V01"}
    assert any(fragment in i.message for i in issues), [i.message for i in issues]


def test_dates_stay_text() -> None:
    text = (EXAMPLES / "lending/metrics/npf_ratio.yaml").read_text("utf-8")
    unquoted = text.replace('"2025-10-01"', "2025-10-01")
    assert unquoted != text
    file, issues = load_text("lending/metrics/npf_ratio.yaml", unquoted)
    assert issues == []
    assert file.definition is not None


def test_line_numbers_point_at_the_problem() -> None:
    text = (EXAMPLES / "lending/metrics/npf_ratio.yaml").read_text("utf-8")
    broken = text.replace("default_grain: month", "default_grain: decade")
    _, issues = load_text("lending/metrics/npf_ratio.yaml", broken)
    assert [i.code for i in issues] == ["V01"]
    line = issues[0].line
    assert line is not None
    assert broken.splitlines()[line - 1].startswith("default_grain")


def test_a_nested_problem_points_at_the_nested_line() -> None:
    text = (EXAMPLES / "lending/entities/customer.yaml").read_text("utf-8")
    broken = text.replace(
        "type: categorical\n    description: Marketing", "type: text\n    description: Marketing"
    )
    assert broken != text
    _, issues = load_text("lending/entities/customer.yaml", broken)
    assert len(issues) == 1
    assert issues[0].line is not None
    assert broken.splitlines()[issues[0].line - 1].strip() == "type: text"


def test_hidden_directories_other_files_and_proposals(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.yaml").write_text("not: a definition", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    domain = (EXAMPLES / "lending/domain.yaml").read_text("utf-8")
    (tmp_path / "lending").mkdir()
    (tmp_path / "lending" / "domain.yaml").write_text(domain, encoding="utf-8")
    (tmp_path / "lending" / "domain.draft.yaml").write_text(
        domain.replace("approved", "draft"), encoding="utf-8"
    )
    definitions = load_definitions(tmp_path)
    assert [(f.path, f.is_proposal) for f in definitions.files] == [
        ("lending/domain.draft.yaml", True),
        ("lending/domain.yaml", False),
    ]
    assert validate(definitions) == []


def test_a_yml_file_and_a_binary_file_are_reported(tmp_path: Path) -> None:
    (tmp_path / "a.yml").write_text("x: 1", encoding="utf-8")
    (tmp_path / "b.yaml").write_bytes(b"\xff\xfe\x00bad")
    codes = {(i.file, i.code) for i in validate(load_definitions(tmp_path))}
    assert codes == {("a.yml", "V02"), ("b.yaml", "V01")}


def test_a_missing_directory_is_one_problem(tmp_path: Path) -> None:
    issues = validate(load_definitions(tmp_path / "nowhere"))
    assert [i.code for i in issues] == ["V01"]


def test_files_are_read_in_sorted_order(tmp_path: Path) -> None:
    first = [f.path for f in load_definitions(EXAMPLES).files]
    second = [f.path for f in load_definitions(EXAMPLES).files]
    assert first == second == sorted(first)
