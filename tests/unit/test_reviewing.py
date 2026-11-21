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
from makna.web.common import visible

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
    assert len(found["dimension_expr"]) < 300 and "…" in found["dimension_expr"]


# --- the comparison --------------------------------------------------------------------------


def test_count_changes_counts_a_changed_line_as_one_removed_and_one_added() -> None:
    assert difference.count_changes("a\nb\nc\n", "a\nB\nc\nd\n") == (2, 1)
    assert difference.count_changes("a\n", "a\n") == (0, 0)
    assert difference.count_changes("", "a\nb\n") == (2, 0)
    big = "line\n" * (difference.MAX_DIFF_LINES + 1)
    assert difference.count_changes(big, "x\n") is None
    assert difference.build_diff(big, "x\n") is None


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
    assert visible("plain text, tabs\tand é and 漢字") == "plain text, tabs\tand é and 漢字"
    assert visible(f"a{chr(0x202E)}b") == "a<U+202E>b"
    assert visible(f"a{chr(0x200B)}b{chr(0xFEFF)}") == "a<U+200B>b<U+FEFF>"
    assert visible(f"x{chr(0x00AD)}y") == "x<U+00AD>y"  # the soft hyphen
    assert visible("a\x1b[31m") == "a<U+001B>[31m"
    assert visible("a b c") == "a<U+2028>b<U+2029>c"
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
    assert repo.subject(sha, 5) == "Fix‮ "[:5]
    with pytest.raises(ValueError):
        repo.subject("not-a-sha")
