"""WP-03 criterion 2: for each rule V01 to V18, a fixture that breaks only that rule."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from makna.definitions.loader import load_definitions
from makna.definitions.validate import validate

from . import definition_fixtures as fx
from .definition_fixtures import Files, Mutation


def _codes_of(mutation: Mutation, tmp_path: Path) -> list[str]:
    root = fx.fixture_dir(mutation, tmp_path)
    return [i.code for i in validate(load_definitions(root))]


def _messages_of(mutation: Mutation, tmp_path: Path) -> list[str]:
    root = fx.fixture_dir(mutation, tmp_path)
    return [i.format() for i in validate(load_definitions(root))]


def test_the_base_repository_is_valid(tmp_path: Path) -> None:
    root = fx.write(copy.deepcopy(fx.base()), tmp_path)
    assert [i.format() for i in validate(load_definitions(root))] == []


def test_every_rule_from_v01_to_v18_and_v20_has_a_fixture() -> None:
    assert sorted(fx.FIXTURES) == [f"V{n:02d}" for n in (*range(1, 19), 20)]


CASES = [
    pytest.param(code, mutation, id=f"{code}-{mutation.__name__}")
    for code, mutations in fx.FIXTURES.items()
    for mutation in mutations
]


@pytest.mark.parametrize(("code", "mutation"), CASES)
def test_a_fixture_breaks_exactly_one_rule(code: str, mutation: Mutation, tmp_path: Path) -> None:
    messages = _messages_of(mutation, tmp_path)
    codes = {m.split(": ", 1)[1].split(" ", 1)[0] for m in messages}
    assert codes == {code}, messages


def test_each_fixture_name_starts_with_its_rule() -> None:
    for code, mutations in fx.FIXTURES.items():
        assert all(m.__name__.startswith(code.lower() + "_") for m in mutations), code


def test_a_problem_names_its_file_and_line(tmp_path: Path) -> None:
    messages = _messages_of(fx.v06_time_dimension_does_not_exist, tmp_path)
    assert len(messages) == 1
    assert messages[0].startswith("shop/entities/order.yaml:")
    path, line, rest = messages[0].split(":", 2)
    assert line.strip().isdigit()
    assert rest.strip().startswith("V06 ")
    text = (tmp_path / path).read_text("utf-8").splitlines()
    assert "time_dimension: shipped_date" in text[int(line) - 1]


def test_a_proposal_for_an_existing_object_is_not_a_duplicate(tmp_path: Path) -> None:
    def propose(files: Files) -> None:
        proposal = copy.deepcopy(files["shop/metrics/order_count.yaml"])
        proposal["status"] = "in_review"
        proposal["description"] = "A changed description for the order count metric."
        files["shop/metrics/order_count.draft.yaml"] = proposal

    assert _codes_of(propose, tmp_path) == []


def test_a_proposal_is_checked_with_its_own_content(tmp_path: Path) -> None:
    def propose(files: Files) -> None:
        proposal = copy.deepcopy(files["shop/entities/region.yaml"])
        proposal["status"] = "draft"
        proposal["dimensions"][0]["expr"] = "COUNT(region_name)"
        files["shop/entities/region.draft.yaml"] = proposal

    messages = _messages_of(propose, tmp_path)
    assert len(messages) == 1
    assert messages[0].startswith("shop/entities/region.draft.yaml:")
    assert " V05 " in messages[0]


def test_draft_metrics_may_depend_on_draft_entities(tmp_path: Path) -> None:
    def draft_metric_on_draft_entity(files: Files) -> None:
        region = files.pop("shop/entities/region.yaml")
        region["status"] = "draft"
        files["shop/entities/region.draft.yaml"] = region
        # Everything built on the draft entity is a draft too.
        for name in ("revenue", "revenue_per_order", "average_order_value"):
            doc = files.pop(f"shop/metrics/{name}.yaml")
            doc["status"] = "draft"
            files[f"shop/metrics/{name}.draft.yaml"] = doc

    assert _codes_of(draft_metric_on_draft_entity, tmp_path) == []


def test_v19_is_left_to_the_compiler_but_has_a_seam(tmp_path: Path) -> None:
    root = fx.write(copy.deepcopy(fx.base()), tmp_path)
    definitions = load_definitions(root)
    from makna.definitions.issues import Issue

    def pretend_v19(_: object) -> list[Issue]:
        return [Issue("shop/metrics/revenue.yaml", None, "V19", "does not compile")]

    assert [i.code for i in validate(definitions)] == []
    assert [i.code for i in validate(definitions, extra_checks=[pretend_v19])] == ["V19"]
