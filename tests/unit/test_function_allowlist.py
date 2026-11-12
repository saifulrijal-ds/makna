"""Fix round 1 (safety I-2): a definition expression may call only functions on an allow-list.

`set_config` and `current_setting` let two approved dimensions carry a raw name from one to the
other, past every check on columns. V05 refuses any function that is not listed, and the final
check refuses one in the finished tree.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from makna.definitions.loader import load_definitions
from makna.definitions.validate import validate
from makna.model.functions import (
    ALLOWED_ANONYMOUS,
    ALLOWED_NODES,
    first_disallowed_function,
    is_allowed_function,
)

from .compiler_support import DEFINITIONS


def parsed(sql: str) -> exp.Expr:
    return sqlglot.parse_one(sql, into=exp.Condition)


def with_dimensions(tmp_path: Path, *dimensions: str) -> Path:
    root = tmp_path / "defs"
    shutil.copytree(DEFINITIONS, root)
    customer = root / "lending" / "entities" / "customer.yaml"
    text = customer.read_text()
    assert "  - name: gender\n" in text
    customer.write_text(
        text.replace("  - name: gender\n", "".join(dimensions) + "  - name: gender\n", 1)
    )
    return root


def dimension(name: str, expr: str, extra: str = "") -> str:
    return (
        f'  - name: {name}\n    type: categorical\n    expr: "{expr}"\n'
        f"    description: A probe dimension for the function check.\n{extra}"
    )


def v05(root: Path) -> list[str]:
    return [i.message for i in validate(load_definitions(root)) if i.code == "V05"]


def test_the_two_probe_dimensions_of_the_safety_review_are_refused(tmp_path: Path) -> None:
    root = with_dimensions(
        tmp_path,
        dimension(
            "year_probe",
            "CASE WHEN set_config('makna.k', full_name, FALSE) IS NOT NULL THEN birth_date END",
            "    sensitivity: personal\n    masking: {method: truncate_date, grain: year}\n",
        ).replace("type: categorical", "type: time"),
        dimension(
            "probe_out",
            "current_setting('makna.k', TRUE) || CASE WHEN customer_id IS NULL THEN 'x' END",
            "    sensitivity: internal\n",
        ),
    )
    messages = v05(root)
    assert any("set_config" in m for m in messages), messages
    assert any("current_setting" in m for m in messages), messages
    assert all("not on the list of functions" in m and "ask the maintainers" in m for m in messages)


def test_the_sample_definitions_pass_v05() -> None:
    assert not [i for i in validate(load_definitions(DEFINITIONS)) if i.code == "V05"]


@pytest.mark.parametrize(
    "sql",
    [
        "CAST(a AS DATE)",
        "COALESCE(a, b)",
        "NULLIF(a, 0)",
        "CASE WHEN a > 1 THEN 'x' ELSE 'y' END",
        "LOWER(a) || UPPER(b)",
        "TRIM(a)",
        "SUBSTRING(a, 1, 3)",
        "LENGTH(a)",
        "CONCAT(a, b)",
        "ROUND(a, 2) + FLOOR(b) + CEIL(c) + ABS(d)",
        "DATE_TRUNC('month', d)",
        "EXTRACT(year FROM d)",
        "a IN (1, 2) AND b BETWEEN 1 AND 2 OR c IS NULL",
        "a::text",
    ],
)
def test_ordinary_pure_functions_are_allowed(sql: str) -> None:
    assert first_disallowed_function(parsed(sql)) is None


@pytest.mark.parametrize(
    ("sql", "name"),
    [
        ("set_config('a', b, FALSE)", "set_config"),
        ("SET_CONFIG('a', b, FALSE)", "set_config"),
        ("current_setting('a')", "current_setting"),
        ("pg_read_file('a')", "pg_read_file"),
        ("nextval('s')", "nextval"),
        ("to_jsonb(a)", "to_jsonb"),
        ("random()", "rand"),
        ("now()", "now"),
        ("lower(set_config('a', b, FALSE))", "set_config"),
    ],
)
def test_other_functions_are_refused_and_named(sql: str, name: str) -> None:
    assert first_disallowed_function(parsed(sql)) == name


def test_an_anonymous_function_is_matched_case_insensitively() -> None:
    assert "btrim" in ALLOWED_ANONYMOUS
    assert is_allowed_function(parsed("BTRIM(a)"))
    assert is_allowed_function(parsed("btrim(a)"))
    assert not is_allowed_function(parsed("BtRiM2(a)"))


def test_the_list_holds_no_aggregate_and_no_window() -> None:
    for node in ALLOWED_NODES:
        assert not issubclass(node, (exp.AggFunc, exp.Window)), node
