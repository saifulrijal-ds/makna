"""WP-15C (Q1, open question 1): a measure takes a list of rules, and every rule must hold.

The planner keeps the rules in the order the definition lists them, SQL generation applies each
one, the explanation names each one, and the older single `rule:` key still reads as a list of one.
"""

from __future__ import annotations

import duckdb

from makna.compiler.compile import compile_request
from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import build_tree
from makna.model.definitions import Entity, Measure

from .compiler_support import lending, request
from .duckdb_support import run_tree

NPF = "npf_outstanding_principal"
JUNE = {"time_start": "2026-06-01", "time_end": "2026-07-01"}


def _with_rules(model: Model, measure: str, **change: object) -> Model:
    """A copy of the sample model in which one measure of `contract_snapshot` is changed."""
    entities: list[Entity] = []
    for entity in model.entities.values():
        if entity.name == "contract_snapshot":
            measures: list[Measure] = [
                m.model_copy(update=change) if m.name == measure else m
                for m in entity.measures or []
            ]
            entity = entity.model_copy(update={"measures": measures})
        entities.append(entity)
    return Model.build(entities, model.metrics.values())


def test_the_sql_of_a_measure_with_two_rules_applies_both() -> None:
    sql = compile_request(lending(), request(NPF, **JUNE), "postgres").sql
    assert '"t0"."days_past_due" > 90' in sql  # the first rule, non_performing
    assert 'COALESCE("t0"."days_past_due", 0) <= 210' in sql  # the second, in_book
    assert sql.index("> 90") < sql.index("<= 210")  # in the order the measure lists them


def test_dropping_the_second_rule_changes_the_number_so_both_are_applied(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    def total(model: Model) -> float:
        rows = run_tree(lending_db, build_tree(model, plan_request(model, request(NPF, **JUNE))))
        return float(rows[0][-1])

    # More than 30 days past due is one rule and more than 90 the other: a row that has only the
    # first (31 to 90 days) must not count, so the two rules together are fewer rows than one.
    one = _with_rules(lending(), NPF, rules=["past_due_30"])
    two = _with_rules(lending(), NPF, rules=["past_due_30", "non_performing"])
    assert total(two) < total(one)


def test_a_row_must_satisfy_every_rule_not_any_of_them(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    sql = compile_request(lending(), request(NPF, **JUNE), "duckdb").sql
    result = lending_db.execute(sql).fetchall()
    expected = lending_db.execute(
        "SELECT SUM(outstanding_principal) FROM contract_monthly_snapshots "
        "WHERE snapshot_date = DATE '2026-06-30' AND days_past_due > 90 "
        "AND COALESCE(days_past_due, 0) <= 210"
    ).fetchall()
    assert result == expected


def test_the_plan_keeps_the_rules_in_the_order_of_the_definition() -> None:
    plan = plan_request(lending(), request(NPF))
    assert [r.name for r in plan.inputs[0].measure_rules] == ["non_performing", "in_book"]
    swapped = _with_rules(lending(), NPF, rules=["in_book", "non_performing"])
    plan = plan_request(swapped, request(NPF))
    assert [r.name for r in plan.inputs[0].measure_rules] == ["in_book", "non_performing"]


def test_the_older_single_rule_key_compiles_as_a_list_of_one() -> None:
    older = _with_rules(lending(), NPF, rules=None, rule="non_performing")
    newer = _with_rules(lending(), NPF, rules=["non_performing"], rule=None)
    got = compile_request(older, request(NPF, **JUNE), "postgres")
    assert got == compile_request(newer, request(NPF, **JUNE), "postgres")
    assert '"t0"."days_past_due" > 90' in got.sql
    assert "<= 210" not in got.sql


def test_the_explanation_names_every_rule_of_the_measure() -> None:
    response = compile_request(lending(), request(NPF, **JUNE), "postgres")
    named = [s for s in response.explanation if "(part of the measure)" in s]
    assert len(named) == 2
    assert "contract_snapshot.non_performing" in named[0]
    assert "contract_snapshot.in_book" in named[1]


def test_a_measure_without_rules_has_none() -> None:
    plan = plan_request(lending(), request("disbursement_amount"))
    assert plan.inputs[0].measure_rules == ()
