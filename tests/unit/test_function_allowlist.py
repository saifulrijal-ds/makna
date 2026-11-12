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
    assert first_disallowed_function(parsed(sql)) == "the function " + name


def test_an_anonymous_function_is_matched_case_insensitively() -> None:
    assert "btrim" in ALLOWED_ANONYMOUS
    assert is_allowed_function(parsed("BTRIM(a)"))
    assert is_allowed_function(parsed("btrim(a)"))
    assert not is_allowed_function(parsed("BtRiM2(a)"))


def test_the_list_holds_no_aggregate_and_no_window() -> None:
    for node in ALLOWED_NODES:
        assert not issubclass(node, (exp.AggFunc, exp.Window)), node


@pytest.mark.parametrize(
    "sql",
    [
        "CAST(a AS INTEGER)", "CAST(a AS BIGINT)", "CAST(a AS DECIMAL(10, 2))", "CAST(a AS REAL)",
        "CAST(a AS DOUBLE PRECISION)", "CAST(a AS TEXT)", "CAST(a AS VARCHAR(20))",
        "CAST(a AS CHAR(3))", "CAST(a AS DATE)", "CAST(a AS TIMESTAMP)",
        "CAST(a AS TIMESTAMPTZ)", "CAST(a AS BOOLEAN)", "a::text",
    ],
)  # fmt: skip
def test_casts_to_plain_types_are_allowed(sql: str) -> None:
    assert first_disallowed_function(parsed(sql)) is None


@pytest.mark.parametrize(
    "sql",
    [
        "a::regclass",
        "CAST(a AS regproc)",
        "CAST(a AS JSON)",
        "CAST(a AS regclass)",
        "CAST(a AS xml)",
    ],
)
def test_casts_to_other_types_are_refused_and_name_the_type(sql: str) -> None:
    found = first_disallowed_function(parsed(sql))
    assert found is not None and found.startswith("a cast to the type")


@pytest.mark.parametrize(
    "sql", ["public.btrim(a)", "pg_catalog.lower(a)", "anyschema.date_part('year', d)"]
)
def test_a_schema_qualified_function_name_is_refused(sql: str) -> None:
    found = first_disallowed_function(parsed(sql))
    assert found is not None and found.startswith("the schema-qualified function")


def probe_cast(tmp_path: Path, target: str) -> Path:
    return with_dimensions(
        tmp_path,
        dimension(
            "year_cast",
            f"CASE WHEN CAST(national_id AS {target}) IS NOT NULL THEN birth_date END",
            "    sensitivity: personal\n    masking: {method: truncate_date, grain: year}\n",
        ).replace("type: categorical", "type: time"),
    )


def test_the_regclass_probe_dimension_fails_v05(tmp_path: Path) -> None:
    messages = v05(probe_cast(tmp_path, "regclass"))
    assert any("a cast to the type" in m and "regclass" in m for m in messages), messages


def test_a_cast_to_an_integer_stays_allowed_and_is_a_documented_limit(tmp_path: Path) -> None:
    """`CAST(personal AS INTEGER)` is an ordinary cast, so the list cannot refuse it, and the
    engine's error text for a value that does not fit can hold the value. DR-5 and 7.6 say so:
    approved definitions are trusted, and what runs the SQL must not pass engine errors on."""
    assert v05(probe_cast(tmp_path, "INTEGER")) == []


def test_verify_refuses_a_cast_to_an_unlisted_type_and_a_qualified_function() -> None:
    from makna.compiler.governance import GovernanceError, verify
    from makna.compiler.plan import plan_request

    from .compiler_support import lending, request

    the_plan = plan_request(lending(), request("new_customer_count"))
    for sql in (
        "CAST(province AS regclass)",
        "public.btrim(province)",
        "CAST(province AS INTEGER)",
    ):
        tree = exp.select(parsed(sql).as_("x")).from_("customers")
        if sql.endswith("INTEGER)"):
            verify(tree, the_plan)  # an ordinary cast passes
            continue
        with pytest.raises(GovernanceError):
            verify(tree, the_plan)
