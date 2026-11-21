"""WP-19B, unit level: what the review pages compute from parsed models
(`makna.services.reviewing`), the comparison of two texts (`makna.services.difference`),
`visible` (format characters made visible), and the commit subject (`GitRepo.subject`). The pages
themselves are tested in `tests/integration/test_web_review.py`."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from makna.definitions.repo import GitRepo
from makna.model.definitions import AnyDefinition, Entity, Metric, parse_definition
from makna.services import difference, reviewing
from makna.web.common import codepoints, visible

ENTITY: dict[str, Any] = {
    "apiVersion": "makna/v1",
    "kind": "entity",
    "name": "person",
    "label": "Person",
    "description": "A person known to the lender, one row per person in the table.",
    "owner": {"team": "credit-risk"},
    "status": "approved",
    "sensitivity": "confidential",
    "domain": "lending",
    "version": 1,
    "synonyms": {"en": ["person"], "id": ["orang"]},
    "source": {"table": "lending.core.persons"},
    "primary_key": ["person_id"],
    "dimensions": [
        {"name": "city", "type": "categorical", "description": "The city the person lives in."},
        {
            "name": "birth_year",
            "type": "time",
            "expr": "birth_date",
            "description": "The year the person was born.",
            "sensitivity": "personal",
            "masking": {"method": "truncate_date", "grain": "year"},
        },
    ],
    "measures": [
        {
            "name": "people",
            "agg": "count",
            "time_dimension": "city",
            "description": "People counted once each.",
            "rules": ["adult"],
        },
        {
            "name": "all_people",
            "agg": "count",
            "time_dimension": "city",
            "description": "Every person, with no rule.",
        },
    ],
    "relationships": [
        {
            "name": "home",
            "to": "branch",
            "keys": [{"from": "branch_id", "to": "branch_id"}],
            "description": "The home branch.",
        }
    ],
    "rules": [
        {"name": "adult", "description": "A person of at least 18 years.", "expr": "age >= 18"},
        {"name": "active", "description": "A person who is active.", "expr": "is_active"},
    ],
}


def metric(name: str, **fields: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": "makna/v1",
        "kind": "metric",
        "name": name,
        "label": name.title(),
        "description": f"The {name} of the lender, written for a business reader.",
        "owner": {"team": "credit-risk"},
        "status": "approved",
        "sensitivity": "internal",
        "domain": "lending",
        "version": 1,
        "synonyms": {"en": [name], "id": [name]},
        "type": "simple",
        "measure": "person.people",
        "format": {"kind": "number"},
    }
    for key, value in fields.items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    return doc


def entity(**changes: Any) -> Entity:
    doc = copy.deepcopy(ENTITY)
    doc.update(changes)
    parsed = parse_definition(doc)
    assert isinstance(parsed, Entity)
    return parsed


def parsed_metric(name: str, **fields: Any) -> Metric:
    parsed = parse_definition(metric(name, **fields))
    assert isinstance(parsed, Metric)
    return parsed


def edited(change: Any) -> Entity:
    doc = copy.deepcopy(ENTITY)
    change(doc)
    parsed = parse_definition(doc)
    assert isinstance(parsed, Entity)
    return parsed


# --- the rules of an entity ------------------------------------------------------------------


def test_added_changed_and_removed_rules_are_found_by_model_not_by_text() -> None:
    official = entity()

    def edit(doc: dict[str, Any]) -> None:
        doc["rules"][0]["expr"] = "age >= 21"  # adult: changed
        doc["rules"] = [r for r in doc["rules"] if r["name"] != "active"]  # removed
        doc["rules"].append({"name": "new_one", "description": "A new rule to add.", "expr": "x"})

    assert reviewing.changed_rules(official, edited(edit)) == [
        ("active", "removed"),
        ("adult", "changed"),
        ("new_one", "added"),
    ]
    # The same model is no change, whatever the text it came from.
    assert reviewing.changed_rules(official, parse_definition(copy.deepcopy(ENTITY))) == []
    # A new entity adds all its rules; anything that is not an entity has none.
    assert reviewing.changed_rules(None, official) == [("active", "added"), ("adult", "added")]
    assert reviewing.changed_rules(None, parsed_metric("mm")) == []


def test_a_rule_is_used_by_the_measures_that_name_it_and_the_metrics_built_on_them() -> None:
    official = entity()
    proposal = edited(lambda d: d["rules"][0].update(expr="age >= 21"))
    direct = parsed_metric("direct")  # measure person.people, which has the rule adult
    plain = parsed_metric("plain", measure="person.all_people")  # a measure with no rule
    filtered = parsed_metric(
        "filtered", measure="person.all_people", filters=[{"rule": "person.adult"}]
    )
    ratio = parsed_metric(
        "ratio", type="ratio", numerator="direct", denominator="plain", measure=None
    )
    derived = parsed_metric(
        "derived", type="derived", expr="ratio + plain", inputs=["ratio", "plain"], measure=None
    )
    officials: dict[str, AnyDefinition] = {
        "entity:person": official,
        "metric:direct": direct,
        "metric:plain": plain,
        "metric:filtered": filtered,
        "metric:ratio": ratio,
        "metric:derived": derived,
    }
    uses, total = reviewing.rule_uses(officials, official, proposal)
    assert total == 1
    (use,) = uses
    assert (use.rule, use.change) == ("person.adult", "changed")
    assert use.measures == ("person.people",)
    # `plain` has neither the measure nor the filter; a ratio and a derived metric use the rule
    # through their inputs.
    assert use.metrics == ("derived", "direct", "filtered", "ratio")
    assert (use.measures_total, use.metrics_total) == (1, 4)


def test_the_proposals_measures_count_as_well_as_the_official_ones() -> None:
    official = entity()

    def edit(doc: dict[str, Any]) -> None:
        doc["rules"][1]["expr"] = "is_active = TRUE"  # `active` changes
        doc["measures"][1]["rules"] = ["active"]  # all_people uses it, in the proposal only

    plain = parsed_metric("plain", measure="person.all_people")
    officials: dict[str, AnyDefinition] = {"entity:person": official, "metric:plain": plain}
    (use,) = reviewing.rule_uses(officials, official, edited(edit))[0]
    assert use.measures == ("person.all_people",)
    # The metric reads the measure the entity will have after approval: it uses the rule then.
    assert use.metrics == ("plain",)
    # A use that the proposal removes is listed too, from the official version.
    removed = edited(
        lambda d: (d["measures"][0].pop("rules"), d["rules"][0].update(expr="age >= 99"))
    )
    direct = parsed_metric("direct")
    (gone,) = reviewing.rule_uses(
        {"entity:person": official, "metric:direct": direct}, official, removed
    )[0]
    assert gone.rule == "person.adult"
    assert gone.measures == ("person.people",) and gone.metrics == ("direct",)


def test_the_users_of_a_rule_are_cut_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    official = entity()
    proposal = edited(lambda d: d["rules"][0].update(expr="age >= 21"))
    officials: dict[str, AnyDefinition] = {"entity:person": official}
    for index in range(7):
        officials[f"metric:m{index}"] = parsed_metric(f"m{index}")
    monkeypatch.setattr(reviewing, "MAX_RULE_USERS", 3)
    (use,) = reviewing.rule_uses(officials, official, proposal)[0]
    assert len(use.metrics) == 3 and use.metrics_total == 7


def test_a_metric_that_reads_itself_through_its_inputs_does_not_hang() -> None:
    a = parsed_metric("aa", type="ratio", numerator="bb", denominator="bb", measure=None)
    b = parsed_metric("bb", type="ratio", numerator="aa", denominator="aa", measure=None)
    found = reviewing.rules_by_metric({"person": entity()}, {"aa": a, "bb": b})
    assert found == {"aa": set(), "bb": set()}


# --- the marks -------------------------------------------------------------------------------


def kinds(official: AnyDefinition | None, proposal: AnyDefinition) -> dict[str, str]:
    return {m.kind: m.text for m in reviewing.definition_marks(official, proposal)}


def test_removed_renamed_and_changed_dimensions_are_marked() -> None:
    official = entity()

    def edit(doc: dict[str, Any]) -> None:
        doc["dimensions"][1]["name"] = "year_of_birth"  # renamed: same expr
        doc["dimensions"][0]["expr"] = "town"  # changed expr
        doc["dimensions"][0]["sensitivity"] = "public"  # changed level

    found = kinds(official, edited(edit))
    assert found["dimension_renamed"] == "dimension birth_year was renamed to year_of_birth"
    assert found["dimension_expr"] == "dimension city: expr changed from (its column) to town"
    assert found["dimension_sensitivity"] == (
        "dimension city: sensitivity changed from confidential to public"
    )
    removed = edited(lambda d: d["dimensions"].pop(0))
    assert kinds(official, removed)["dimension_removed"] == "dimension city was removed"
    masking = edited(
        lambda d: d["dimensions"][1].update(masking={"method": "truncate_date", "grain": "month"})
    )
    assert kinds(official, masking)["dimension_masking"] == (
        "dimension birth_year: masking changed from truncate_date(year) to truncate_date(month)"
    )
    unmasked = edited(lambda d: d["dimensions"][1].pop("masking"))
    assert "from truncate_date(year) to none" in kinds(official, unmasked)["dimension_masking"]


def test_a_dimension_that_reads_the_column_of_a_personal_one_is_marked() -> None:
    official = entity()
    sibling = {
        "name": "birth_month",
        "type": "time",
        "expr": "birth_date",
        "description": "The month the person was born.",
        "sensitivity": "internal",
    }
    found = kinds(official, edited(lambda d: d["dimensions"].append(sibling)))
    assert found["same_column_as_personal"] == (
        "dimension birth_month reads the column birth_date, which the personal dimension "
        "birth_year reads too"
    )
    # A dimension that was already there and reads what it read is not marked again.
    assert "same_column_as_personal" not in kinds(official, entity())
    # A personal dimension that is itself new is not compared with itself.
    twin = {**sibling, "name": "birth_two", "sensitivity": "personal"}
    found = kinds(official, edited(lambda d: d["dimensions"].append(twin)))
    assert "birth_year" in found["same_column_as_personal"]


def test_relationships_and_the_owner_are_marked() -> None:
    official = entity()

    def edit(doc: dict[str, Any]) -> None:
        doc["relationships"].append(
            {
                "name": "work",
                "to": "branch",
                "keys": [{"from": "work_id", "to": "branch_id"}],
                "description": "The branch the person works at.",
            }
        )
        doc["owner"] = {"team": "sales"}

    found = kinds(official, edited(edit))
    assert found["relationship_added"] == "relationship work was added"
    assert found["owner"] == "owner team changes from credit-risk to sales"
    removed = edited(lambda d: d.update(relationships=[]))
    assert kinds(official, removed)["relationship_removed"] == "relationship home was removed"
    changed = edited(lambda d: d["relationships"][0].update(to="region"))
    assert kinds(official, changed)["relationship_changed"] == "relationship home was changed"
    assert reviewing.definition_marks(None, official) == []
    assert reviewing.definition_marks(official, entity()) == []


def test_a_name_or_expression_in_a_mark_is_cut() -> None:
    long = "x" * 500
    found = kinds(entity(), edited(lambda d: d["dimensions"][0].update(expr=long)))
    assert len(found["dimension_expr"]) < 300 and "\u2026" in found["dimension_expr"]


# --- the comparison --------------------------------------------------------------------------


def test_count_changes_counts_a_changed_line_as_one_removed_and_one_added() -> None:
    assert difference.count_changes("a\nb\nc\n", "a\nB\nc\nd\n") == (2, 1)
    assert difference.count_changes("a\n", "a\n") == (0, 0)
    assert difference.count_changes("", "a\nb\n") == (2, 0)
    # A moved line is neither: the count is of lines, as multisets, and does not compare texts.
    assert difference.count_changes("a\nb\n", "b\na\n") == (0, 0)
    assert difference.count_changes("a\na\n", "a\n") == (0, 1)


def test_the_rows_of_a_difference_are_plain_strings_and_unchanged_runs_collapse() -> None:
    before = "".join(f"line {i}\n" for i in range(40))
    after = before.replace("line 20\n", "<b>line 20</b>\n")
    rows = difference.build_diff(before, after)
    assert rows is not None
    assert [r.kind for r in rows if r.kind not in ("same",)] == ["skipped", "changed", "skipped"]
    changed = next(r for r in rows if r.kind == "changed")
    assert changed.right == "<b>line 20</b>" and isinstance(changed.right, str)
    assert difference.build_diff(before, before) == []


# --- format characters -----------------------------------------------------------------------


def test_visible_marks_format_control_and_odd_characters_and_nothing_else() -> None:
    assert (
        visible("plain text, tabs\tand \u00e9 and \u6f22\u5b57")
        == "plain text, tabs\tand \u00e9 and \u6f22\u5b57"
    )
    assert visible(f"a{chr(0x202E)}b") == "a<U+202E>b"
    assert visible(f"a{chr(0x200B)}b{chr(0xFEFF)}") == "a<U+200B>b<U+FEFF>"
    assert visible(f"x{chr(0x00AD)}y") == "x<U+00AD>y"  # the soft hyphen
    assert visible("a\x1b[31m") == "a<U+001B>[31m"
    assert visible("a\u2028b\u2029c") == "a<U+2028>b<U+2029>c"
    assert visible("lone\ud800surrogate") == "lone<U+D800>surrogate"
    assert visible(None) == "None" and visible(12) == "12"
    assert visible(chr(0xE0041)) == "<U+E0041>"  # a tag character


# --- the commit subject ----------------------------------------------------------------------


def _git(path: Path, *args: str, message: str | None = None) -> None:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "A",
        "GIT_AUTHOR_EMAIL": "a@example.com",
        "GIT_COMMITTER_NAME": "A",
        "GIT_COMMITTER_EMAIL": "a@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)


def test_the_subject_is_the_first_line_cut_with_control_characters_replaced(
    tmp_path: Path,
) -> None:
    repo = GitRepo(tmp_path / "r")
    repo.init()
    (tmp_path / "r" / "f.txt").write_text("x")
    _git(tmp_path / "r", "add", "f.txt")
    message = f"Fix{chr(0x202E)} it\tnow\n\nbody line\n"
    _git(tmp_path / "r", "commit", "-q", "-m", message)
    sha = repo.rev_parse("HEAD")
    assert sha is not None
    assert repo.subject(sha) == f"Fix{chr(0x202E)} it now"  # the format character is kept
    assert repo.subject(sha, 5) == "Fix\u202e "[:5]
    with pytest.raises(ValueError):
        repo.subject("not-a-sha")


# --- the cost of a difference (fix round 1) ---------------------------------------------------


def _dimensions(count: int, tag: str) -> str:
    """YAML-shaped lines that repeat, the worst shape for the line matching: `count` dimensions
    with all names changed and the other three lines the same."""
    lines: list[str] = []
    for index in range(count):
        lines += [
            f"  - name: {tag}{index}",
            "    type: categorical",
            "    description: A dimension of the entity.",
            "    sensitivity: internal",
        ]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("dimensions", [125, 150, 400])
def test_the_worst_case_at_and_above_the_bound_is_fast_and_shows_every_line(
    dimensions: int,
) -> None:
    """125 dimensions of 4 lines on each side are 500 x 500 lines, the bound itself (250,000
    cells); 150 and 400 dimensions are above it (the middle is one removed block beside one added
    block)."""
    import time

    before, after = _dimensions(dimensions, "x"), _dimensions(dimensions, "y")
    started = time.perf_counter()
    rows = difference.build_diff(before, after)
    counted = difference.count_changes(before, after)
    assert time.perf_counter() - started < 5  # about 0.25 s here; the limit is generous for CI
    left = [r.left for r in rows if r.left_no is not None]
    right = [r.right for r in rows if r.right_no is not None]
    # Every line of both sides is on the page (only long unchanged runs are collapsed).
    unchanged = sum(r.skipped for r in rows if r.kind == "skipped")
    assert len(left) + unchanged == len(before.splitlines())
    assert len(right) + unchanged == len(after.splitlines())
    changed_left = [r.left for r in rows if r.kind in ("changed", "removed")]
    assert [x for x in before.splitlines() if "name: x" in x] == [
        x for x in changed_left if "name: x" in x
    ]
    assert counted == (dimensions, dimensions)


def test_a_stretch_above_the_bound_is_paired_by_position_says_so_and_shows_equal_pairs_as_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(difference, "MAX_MATCH_CELLS", 4)
    before = "top\na\nb\nc\nd\ne\nbottom\n"
    after = "top\nA\nB\nC\nD\nE\nbottom\n"
    rows = difference.build_diff(before, after)
    assert [r.kind for r in rows if r.kind not in ("same", "skipped")] == [
        "unmatched",
        *["changed"] * 5,
    ]
    assert next(r for r in rows if r.kind == "unmatched").skipped == 5
    # A pair of equal lines inside the block is not a change.
    paired = difference.build_diff("p\nx\na\nx\na\nx\nq\n", "r\nx\na\ny\na\nx\ns\n")
    assert [r.kind for r in paired if r.kind not in ("same", "skipped")] == [
        "unmatched",
        "changed",
        "changed",
        "changed",
    ]
    assert [r.kind for r in paired].count("same") == 4  # equal pairs inside the block
    # One side longer: the extra lines are removed rows.
    longer, shorter = "top\na\nb\nc\nd\ne\nbottom\n", "top\nA\nB\nbottom\n"
    rows = difference.build_diff(longer, shorter)
    assert [r.kind for r in rows if r.kind not in ("same", "skipped")] == [
        "unmatched",
        "changed",
        "changed",
        "removed",
        "removed",
        "removed",
    ]
    assert difference.count_changes(longer, shorter) == (2, 5)


def test_a_changed_line_is_cut_into_the_parts_that_differ() -> None:
    rows = difference.build_diff("level: mass\n", "level: m\u0430ss\n")
    (row,) = [r for r in rows if r.kind == "changed"]
    assert row.left_parts == (("level: m", False), ("a", True), ("ss", False))
    assert row.right_parts == (("level: m", False), ("\u0430", True), ("ss", False))


# --- characters that do not show --------------------------------------------------------------


def test_visible_marks_default_ignorable_characters_and_spaces_that_are_not_a_plain_space() -> None:
    for code in (0x115F, 0x1160, 0x3164, 0xFFA0, 0xFE0F, 0xFE00, 0xE0100, 0xE01EF, 0x034F, 0x2800):
        assert visible(f"a{chr(code)}b") == f"a<U+{code:04X}>b", hex(code)
    assert visible("a\u00a0b\u3000c\u2003d") == "a<U+00A0>b<U+3000>c<U+2003>d"
    assert visible("a b\tc") == "a b\tc"  # a plain space and a tab are what they look like


def test_codepoints_names_a_non_ascii_character_and_leaves_ascii_alone() -> None:
    assert codepoints("mass") == "mass"
    assert codepoints("m\u0430ss") == "m\u0430<U+0430>ss"


def test_the_audit_time_is_shown_in_utc_whatever_zone_the_session_used() -> None:
    from datetime import UTC, datetime, timedelta, timezone

    from makna.web.review import _utc

    jakarta = datetime(2026, 11, 21, 19, 47, 51, tzinfo=timezone(timedelta(hours=7)))
    assert _utc(jakarta) == "2026-11-21 12:47:51"
    assert _utc(datetime(2026, 11, 21, 12, 47, 51, tzinfo=UTC)) == "2026-11-21 12:47:51"
    assert _utc(datetime(2026, 11, 21, 12, 47, 51)) == "2026-11-21 12:47:51"


# --- the measures of an entity that start or stop using a rule --------------------------------


def test_a_measure_that_stops_using_a_rule_is_listed_with_the_metrics_built_on_it() -> None:
    official = entity()
    proposal = edited(lambda d: d["measures"][0].pop("rules"))  # `people` drops the rule `adult`
    direct = parsed_metric("direct")  # measure person.people
    ratio = parsed_metric(
        "ratio", type="ratio", numerator="direct", denominator="plain", measure=None
    )
    plain = parsed_metric("plain", measure="person.all_people")
    officials: dict[str, AnyDefinition] = {
        "entity:person": official,
        "metric:direct": direct,
        "metric:ratio": ratio,
        "metric:plain": plain,
    }
    changes, total = reviewing.measure_rule_changes(officials, official, proposal)
    assert total == 1
    (change,) = changes
    assert (change.measure, change.before, change.after) == ("person.people", ("adult",), ())
    assert change.metrics == ("direct", "ratio") and change.metrics_total == 2
    # A measure that gains one is listed the same way, and an unchanged proposal has none.
    gains = edited(lambda d: d["measures"][1].update(rules=["active"]))
    (gain,) = reviewing.measure_rule_changes(officials, official, gains)[0]
    assert (gain.before, gain.after, gain.metrics) == ((), ("active",), ("plain", "ratio"))
    assert reviewing.measure_rule_changes(officials, official, entity()) == ((), 0)


def test_the_owner_team_mark_ignores_case_as_the_lifecycle_does() -> None:
    official = entity()
    assert "owner" not in kinds(official, edited(lambda d: d.update(owner={"team": "Credit-Risk"})))
    assert "owner" in kinds(official, edited(lambda d: d.update(owner={"team": "sales"})))


# --- a change in a large entity (fix round 2) -------------------------------------------------


def _entity_lines(dimensions: int) -> list[str]:
    lines = ["apiVersion: makna/v1", "kind: entity", "name: wide", "label: Wide", "dimensions:"]
    for index in range(dimensions):
        lines += [
            f"  - name: dimension_{index}",
            "    type: categorical",
            "    values:",
            "      - value: employee",
            "      - value: other",
            "    sensitivity: internal",
            "    description: The same text for every one of them.",
            "    expr: occupation_group",
        ]
    lines += ["relationships:", "  - name: home", "    description: The last line of the file."]
    return lines


def _changes(rows: list[difference.DiffRow]) -> list[difference.DiffRow]:
    return [r for r in rows if r.kind not in ("same", "skipped")]


def _wide_pair(shift: bool) -> tuple[str, str]:
    before = _entity_lines(140)  # over 1100 lines
    after = list(before)
    after[3] = "label: Wide, tidied"
    after[-1] = "    description: The last line of the file, changed."
    middle = next(i for i in range(800, len(after)) if after[i].endswith("value: employee"))
    after[middle] = after[middle].replace("employee", "empl\u043eyee")  # Cyrillic o
    if shift:
        after.insert(5, "synonyms: {}")
    return "\n".join(before) + "\n", "\n".join(after) + "\n"


@pytest.mark.parametrize("shift", [False, True])
def test_three_changes_in_a_large_entity_are_exactly_three_rows_with_the_lookalike_marked(
    shift: bool,
) -> None:
    before, after = _wide_pair(shift)
    assert len(before.splitlines()) > 1000
    rows = difference.build_diff(before, after)
    changes = _changes(rows)
    expected = ["changed", "changed", "changed"] + (["added"] if shift else [])
    assert sorted(r.kind for r in changes) == sorted(expected)
    assert not any(r.kind == "unmatched" for r in rows)
    lookalike = next(r for r in changes if "empl\u043eyee" in r.right)
    assert ("\u043e", True) in lookalike.right_parts and ("o", True) in lookalike.left_parts
    assert difference.count_changes(before, after) == (4 if shift else 3, 3)


def test_the_worst_shape_at_the_bound_is_fast_and_no_anchor_means_one_block() -> None:
    import time

    one, other = "  - value: a", "  - value: b"
    before = "\n".join(["X"] + [one] * 498 + ["Y"]) + "\n"
    after = "\n".join(["Z"] + [one, other] * 249 + ["W"]) + "\n"
    started = time.perf_counter()
    rows = difference.build_diff(before, after)
    assert time.perf_counter() - started < 5  # about 1.5 s here at the bound
    assert (
        len([r for r in rows if r.left_no is not None])
        + sum(r.skipped for r in rows if r.kind == "skipped")
        >= 500
    )


def test_the_character_comparison_has_a_budget_and_a_longest_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines_before = "".join(f"key_{i}: value number {i}\n" for i in range(20))
    lines_after = "".join(f"key_{i}: other number {i}\n" for i in range(20))
    monkeypatch.setattr(difference, "MAX_PARTS_WORK", 3 * 40 * 40)
    rows = [r for r in difference.build_diff(lines_before, lines_after) if r.kind == "changed"]
    assert len(rows) == 20
    with_parts = [r for r in rows if r.left_parts]
    assert 0 < len(with_parts) < 20  # the budget runs out part of the way down the page
    assert all(r.left_parts for r in rows[: len(with_parts)])
    long_line = "x" * (difference.MAX_PARTS_LINE + 1)
    (row,) = [
        r for r in difference.build_diff(f"{long_line}\n", f"{long_line}y\n") if r.kind == "changed"
    ]
    assert row.left_parts == () and row.right_parts == ()


def test_many_gaps_under_the_bound_share_one_budget_for_the_page() -> None:
    """Twenty gaps of 200 x 200 lines (40,000 cells each, the worst repeated shape) between
    lines that are the same on both sides: matching them all would be 800,000 cells. The budget
    of the page (500,000) is spent after twelve, and the rest are blocks with their notice."""
    import time

    repeated, other = "  - value: a", "  - value: b"
    before: list[str] = []
    after: list[str] = []
    for gap in range(20):
        before += [f"anchor_{gap}", f"L{gap}", *[repeated] * 198, f"end_{gap}"]
        after += [f"anchor_{gap}", f"R{gap}", *[repeated, other] * 99, f"stop_{gap}"]
    started = time.perf_counter()
    rows = difference.build_diff("\n".join(before) + "\n", "\n".join(after) + "\n")
    assert time.perf_counter() - started < 5  # about 2 to 3 s here
    notices = [r for r in rows if r.kind == "unmatched"]
    assert 5 <= len(notices) <= 10  # 12 gaps were matched, the others are blocks
    assert all(r.skipped == 200 for r in notices)
    # Every line of both sides is still there.
    assert len([r for r in rows if r.left_no is not None]) + sum(
        r.skipped for r in rows if r.kind == "skipped"
    ) == len(before)


def test_a_total_budget_of_zero_makes_every_gap_a_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(difference, "MAX_MATCH_TOTAL", 0)
    rows = difference.build_diff("top\na\nb\nbottom\n", "top\nA\nB\nbottom\n")
    assert [r.kind for r in rows if r.kind not in ("same", "skipped")] == [
        "unmatched",
        "changed",
        "changed",
    ]
