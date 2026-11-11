"""WP-09 criteria 3 to 7: balances, result columns, derived metrics, literals, dialects, rules."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import duckdb
import pytest
import sqlglot
from sqlglot import exp

from makna.compiler.compile import compile_request
from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import build_tree, to_sql
from makna.model.definitions import Entity, Filter, Metric, RuleRef, parse_definition
from makna.model.request import ErrorCode, QueryError

from .compiler_support import lending, request, tree_of
from .definition_fixtures import SYN
from .duckdb_support import run_sql, run_tree, same_rows

# --------------------------------------------------------------------------------------
# Criterion 3: a balance is the last snapshot date of each period, never a sum over dates


def test_a_balance_over_six_months_with_no_grain_uses_one_snapshot_date(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    tree = tree_of(
        lending(), "outstanding_principal", time_start="2026-01-01", time_end="2026-07-01"
    )
    got = run_tree(lending_db, tree)
    last_month_end = run_sql(
        lending_db,
        "SELECT SUM(outstanding_principal) FROM contract_monthly_snapshots "
        "WHERE snapshot_date = DATE '2026-06-30'",
    )
    all_six = run_sql(
        lending_db,
        "SELECT SUM(outstanding_principal) FROM contract_monthly_snapshots "
        "WHERE snapshot_date >= DATE '2026-01-01' AND snapshot_date < DATE '2026-07-01'",
    )
    assert same_rows(got, last_month_end)
    assert not same_rows(got, all_six), "the data would not catch a sum over months"


def test_a_balance_with_quarter_grain_uses_the_last_month_end_of_each_quarter(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    tree = tree_of(
        lending(),
        "outstanding_principal",
        grain="quarter",
        time_start="2025-10-01",
        time_end="2026-07-01",
    )
    got = run_tree(lending_db, tree)
    want = run_sql(
        lending_db,
        """
        SELECT q, SUM(outstanding_principal) FROM (
          VALUES (DATE '2025-10-01', DATE '2025-12-31'),
                 (DATE '2026-01-01', DATE '2026-03-31'),
                 (DATE '2026-04-01', DATE '2026-06-30')) AS quarters(q, last_month_end)
        JOIN contract_monthly_snapshots s ON s.snapshot_date = quarters.last_month_end
        GROUP BY q ORDER BY q
        """,
    )
    assert len(want) == 3
    assert same_rows(got, want)


def test_a_quarter_cut_short_by_the_range_uses_its_last_snapshot_inside_the_range(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    tree = tree_of(
        lending(),
        "outstanding_principal",
        grain="quarter",
        time_start="2026-01-01",
        time_end="2026-05-15",
    )
    want = run_sql(
        lending_db,
        """
        SELECT q, SUM(outstanding_principal) FROM (
          VALUES (DATE '2026-01-01', DATE '2026-03-31'), (DATE '2026-04-01', DATE '2026-04-30')
        ) AS quarters(q, last_in_range)
        JOIN contract_monthly_snapshots s ON s.snapshot_date = quarters.last_in_range
        GROUP BY q ORDER BY q
        """,
    )
    assert same_rows(run_tree(lending_db, tree), want)


# --------------------------------------------------------------------------------------
# Criterion 4: result columns and their order (section 7.2)


def column_names(db: duckdb.DuckDBPyConnection, tree: exp.Select) -> list[str]:
    cursor = db.execute(to_sql(tree, "duckdb"))
    return [d[0] for d in cursor.description or []]


@pytest.mark.parametrize(
    ("metric", "fields", "expected"),
    [
        (
            "disbursement_amount",
            {"dimensions": ["branch.region_name", "product.financing_scheme"], "grain": "month"},
            ["metric_time", "branch__region_name", "product__financing_scheme"]
            + ["disbursement_amount"],
        ),
        ("disbursement_amount", {}, ["disbursement_amount"]),
        (
            "collection_rate",
            {"dimensions": ["branch.region_name"], "grain": "quarter"},
            ["metric_time", "branch__region_name", "collected_amount", "due_amount"]
            + ["collection_rate"],
        ),
        (
            "approval_rate",
            {"dimensions": ["application.channel"]},
            ["application__channel", "approved_application_count"]
            + ["decided_application_count", "approval_rate"],
        ),
    ],
    ids=["simple with grain", "simple bare", "ratio", "ratio without grain"],
)
def test_result_columns_follow_section_7_2(
    lending_db: duckdb.DuckDBPyConnection,
    metric: str,
    fields: dict[str, Any],
    expected: list[str],
) -> None:
    model = lending()
    plan = plan_request(model, request(metric, **fields))
    tree = build_tree(model, plan)
    assert column_names(lending_db, tree) == expected
    assert [c.name for c in plan.columns] == expected
    response = compile_request(model, request(metric, **fields), "duckdb")
    assert [c.name for c in response.columns] == expected
    roles = [c.role for c in response.columns]
    assert roles[-1] == "metric"
    assert roles[: len(expected) - 1].count("time") == (1 if "metric_time" in expected else 0)


def test_rows_are_ordered_by_the_key_columns(lending_db: duckdb.DuckDBPyConnection) -> None:
    tree = tree_of(
        lending(), "disbursement_amount", dimensions=["branch.region_name"], grain="month"
    )
    rows = run_tree(lending_db, tree)
    keys = [(r[0], r[1] is None, r[1] or "") for r in rows]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------------------
# Criterion 5: derived metrics and a ratio with a zero denominator


def derived(name: str, expr: str, inputs: list[str]) -> Metric:
    found = parse_definition(
        {
            "apiVersion": "makna/v1",
            "kind": "metric",
            "name": name,
            "label": name.replace("_", " ").title(),
            "description": f"A fixture metric called {name}, computed from other metrics.",
            "domain": "lending",
            "owner": {"team": "qa"},
            "status": "approved",
            "sensitivity": "internal",
            "synonyms": SYN,
            "type": "derived",
            "expr": expr,
            "inputs": inputs,
            "format": {"kind": "number"},
        }
    )
    assert isinstance(found, Metric)
    return found


def with_metrics(*extra: Metric) -> Model:
    base = lending()
    return Model.build(base.entities.values(), [*base.metrics.values(), *extra])


JUNE = {"time_start": "2026-01-01", "time_end": "2026-07-01"}


def test_a_derived_metric_subtracts_separately_aggregated_inputs(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    model = with_metrics(
        derived(
            "collected_less_due",
            "collected_amount - due_amount",
            ["collected_amount", "due_amount"],
        )
    )
    tree = tree_of(model, "collected_less_due", dimensions=["branch.region_name"], **JUNE)
    want = run_sql(
        lending_db,
        """
        WITH num AS (
          SELECT b.region_name AS r, SUM(p.principal_paid + p.interest_paid) AS v
          FROM payments p JOIN contracts c ON c.contract_id = p.contract_id
          LEFT JOIN branches b ON b.branch_id = c.servicing_branch_id
          WHERE NOT p.is_reversed AND p.posting_date >= DATE '2026-01-01'
            AND p.posting_date < DATE '2026-07-01' GROUP BY 1),
        den AS (
          SELECT b.region_name AS r, SUM(i.installment_amount) AS v
          FROM installment_schedules i JOIN contracts c ON c.contract_id = i.contract_id
          LEFT JOIN branches b ON b.branch_id = c.servicing_branch_id
          WHERE i.is_current AND i.due_date >= DATE '2026-01-01'
            AND i.due_date < DATE '2026-07-01' GROUP BY 1)
        SELECT COALESCE(num.r, den.r), COALESCE(num.v, 0), COALESCE(den.v, 0),
               COALESCE(num.v, 0) - COALESCE(den.v, 0)
        FROM num FULL JOIN den ON num.r IS NOT DISTINCT FROM den.r ORDER BY 1
        """,
    )
    got = run_tree(lending_db, tree)
    assert len(want) >= 3
    assert same_rows(got, want)
    # The inputs appear as columns before the metric, in the order of the definition.
    assert [c.name for c in plan_request(model, request("collected_less_due")).columns] == [
        "collected_amount",
        "due_amount",
        "collected_less_due",
    ]


def test_a_derived_division_of_counts_is_not_integer_division(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    model = with_metrics(
        derived(
            "approved_share",
            "approved_application_count / NULLIF(decided_application_count, 0)",
            ["approved_application_count", "decided_application_count"],
        )
    )
    tree = tree_of(model, "approved_share")
    postgres = to_sql(tree, "postgres")
    assert "CAST" in postgres and "DOUBLE PRECISION" in postgres
    ((_, _, share),) = run_tree(lending_db, tree)
    ((approved, decided),) = run_sql(
        lending_db,
        "SELECT COUNT(*) FILTER (WHERE sts_putusan = 'approved'), "
        "COUNT(*) FILTER (WHERE sts_putusan IN ('approved', 'rejected')) FROM loan_applications "
        "WHERE tgl_putusan IS NOT NULL",
    )
    assert 0 < float(share) < 1
    assert abs(float(share) - approved / decided) < 1e-9


def test_a_derived_metric_can_use_a_ratio_as_an_input(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    model = with_metrics(
        derived("collection_percent", "collection_rate * 100", ["collection_rate"])
    )
    ratio = run_tree(lending_db, tree_of(model, "collection_rate", **JUNE))
    percent = run_tree(lending_db, tree_of(model, "collection_percent", **JUNE))
    assert percent[0][-1] is not None
    assert abs(float(percent[0][-1]) - 100 * float(ratio[0][-1])) < 1e-6


def test_a_ratio_with_a_zero_denominator_is_null_and_not_an_error(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    # 2024-02-07 has payments but no installment falls due that day.
    tree = tree_of(lending(), "collection_rate", time_start="2024-02-07", time_end="2024-02-08")
    ((collected, due, rate),) = run_tree(lending_db, tree)
    assert collected > 0 and due == 0
    assert rate is None
    # A group that exists on one side only keeps its row, with a NULL ratio.
    grouped = tree_of(
        lending(),
        "collection_rate",
        dimensions=["branch.region_name"],
        time_start="2024-02-07",
        time_end="2024-02-08",
    )
    rows = run_tree(lending_db, grouped)
    assert rows and all(r[-1] is None for r in rows)
    # With nothing at all in the range the ratio is NULL as well.
    empty = tree_of(lending(), "collection_rate", time_start="2035-01-01", time_end="2035-02-01")
    assert run_tree(lending_db, empty) == [(0, 0, None)]


# --------------------------------------------------------------------------------------
# Criterion 6: filter values are literals


HOSTILE = "x'); DROP TABLE contracts; SELECT * FROM customers WHERE 'a'='a"


def literals(tree: exp.Expr) -> list[str]:
    return [lit.this for lit in tree.find_all(exp.Literal) if lit.is_string]


def test_a_hostile_filter_value_returns_nothing_and_stays_one_string_literal(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    tree = tree_of(
        lending(),
        "disbursement_amount",
        dimensions=["branch.branch_name"],
        filters=[{"dimension": "branch.branch_name", "op": "=", "value": HOSTILE}],
    )
    assert run_tree(lending_db, tree) == []
    # The tree holds the text as exactly one string literal, and nothing else mentions it.
    assert literals(tree).count(HOSTILE) == 1
    assert [t for t in literals(tree) if "DROP" in t.upper() or "SELECT" in t.upper()] == [HOSTILE]
    assert not list(tree.find_all(exp.Drop))
    assert len(list(tree.find_all(exp.Select))) == 1
    # The printed SQL, parsed back, is one statement holding the text once, as a literal.
    for dialect in ("postgres", "duckdb"):
        printed = to_sql(tree, dialect)
        statements = sqlglot.parse(printed, read=dialect)
        (statement,) = statements
        assert statement is not None
        assert literals(statement).count(HOSTILE) == 1
        assert printed.count(HOSTILE.replace("'", "''")) == 1


def test_hostile_values_in_a_list_and_with_backslashes_stay_literals(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    values = [HOSTILE, "a\\'; --", "’ OR 1=1 --"]
    tree = tree_of(
        lending(),
        "disbursement_amount",
        dimensions=["branch.branch_name"],
        filters=[{"dimension": "branch.branch_name", "op": "in", "value": values}],
    )
    assert run_tree(lending_db, tree) == []
    assert sorted(literals(tree)) == sorted(values)
    for dialect in ("postgres", "duckdb"):
        statements = sqlglot.parse(to_sql(tree, dialect), read=dialect)
        assert len(statements) == 1


FILTERS: list[tuple[str, dict[str, Any], str]] = [
    ("=", {"dimension": "contract.contract_status", "op": "=", "value": "active"},
     "contract_status = 'active'"),
    ("!=", {"dimension": "contract.contract_status", "op": "!=", "value": "active"},
     "contract_status <> 'active'"),
    (">=", {"dimension": "contract.tenor_months", "op": ">=", "value": 36}, "tenor_months >= 36"),
    ("<", {"dimension": "contract.annual_rate_pct", "op": "<", "value": 12.5},
     "annual_rate_pct < 12.5"),
    ("in", {"dimension": "contract.contract_status", "op": "in", "value": ["active", "closed"]},
     "contract_status IN ('active', 'closed')"),
    ("not_in", {"dimension": "contract.contract_status", "op": "not_in", "value": ["closed"]},
     "contract_status NOT IN ('closed')"),
    ("is_null", {"dimension": "contract.closed_date", "op": "is_null"}, "closed_date IS NULL"),
    ("is_not_null", {"dimension": "contract.closed_date", "op": "is_not_null"},
     "closed_date IS NOT NULL"),
    ("boolean", {"dimension": "contract.is_restructured", "op": "=", "value": True},
     "is_restructured"),
    ("date", {"dimension": "contract.signed_date", "op": ">=", "value": "2025-06-01"},
     "signed_date >= DATE '2025-06-01'"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("flt", "condition"), [f[1:] for f in FILTERS], ids=[f[0] for f in FILTERS]
)
def test_every_filter_operator_selects_the_rows_a_plain_query_selects(
    lending_db: duckdb.DuckDBPyConnection, flt: dict[str, Any], condition: str
) -> None:
    tree = tree_of(lending(), "disbursement_amount", filters=[flt])
    want = run_sql(
        lending_db,
        "SELECT SUM(principal_amount) FROM contracts "
        f"WHERE disbursement_date IS NOT NULL AND {condition}",
    )
    got = run_tree(lending_db, tree)
    assert want[0][0] is not None
    assert same_rows(got, want)


# --------------------------------------------------------------------------------------
# Criterion 7: every approved sample metric, on both dialects; rules equal their inline form


def approved_metrics() -> list[str]:
    return sorted(n for n, m in lending().metrics.items() if m.status == "approved")


@pytest.mark.parametrize("name", approved_metrics())
def test_every_approved_metric_prints_for_postgres_and_runs_on_duckdb(
    lending_db: duckdb.DuckDBPyConnection, name: str
) -> None:
    metric = lending().metrics[name]
    req = request(name, grain=metric.default_grain)
    postgres = compile_request(lending(), req, "postgres")
    parsed = sqlglot.parse_one(postgres.sql, read="postgres")
    assert isinstance(parsed, exp.Select)
    assert [s.alias_or_name for s in parsed.expressions] == [c.name for c in postgres.columns]
    on_duckdb = compile_request(lending(), req, "duckdb")
    cursor = lending_db.execute(on_duckdb.sql)
    assert [d[0] for d in cursor.description or []] == [c.name for c in on_duckdb.columns]
    cursor.fetchall()  # the SQL ran


def inline_rules(model: Model) -> Model:
    """A copy of the sample model in which no rule is used: each condition is written inline."""
    entities: list[Entity] = []
    for entity in model.entities.values():
        measures = [
            m.model_copy(update={"rule": None, "filter": rule_expr(entity, m.rule, m.filter)})
            if m.rule is not None
            else m
            for m in entity.measures or []
        ]
        entities.append(entity.model_copy(update={"measures": measures or None}))
    metrics: list[Metric] = []
    for metric in model.metrics.values():
        filters = [
            Filter(dimension="contract.is_restructured", op="=", value=True)
            if isinstance(f, RuleRef) and f.rule == "contract.restructured"
            else f
            for f in metric.filters or []
        ]
        metrics.append(metric.model_copy(update={"filters": filters or None}))
    return Model.build(entities, metrics)


def rule_expr(entity: Entity, rule: str | None, existing: str | None) -> str:
    found = next(r for r in entity.rules or [] if r.name == rule)
    return found.expr if existing is None else f"({existing}) AND ({found.expr})"


RULE_REQUESTS: list[tuple[str, dict[str, Any]]] = [
    ("npf_outstanding_principal", {"dimensions": ["branch.region_name"], "grain": "quarter"}),
    ("npf_ratio", {"dimensions": ["product.product_category"], "grain": "year"}),
    ("par30_ratio", {"grain": "month", "time_start": "2025-07-01", "time_end": "2026-07-01"}),
    ("restructured_outstanding_principal", {"dimensions": ["branch.region_name"]}),
    ("restructured_ratio", {"dimensions": ["branch.region_name"], "grain": "quarter"}),
]


@pytest.mark.parametrize(("metric", "fields"), RULE_REQUESTS, ids=[r[0] for r in RULE_REQUESTS])
def test_a_metric_using_a_rule_equals_a_copy_with_the_condition_written_inline(
    lending_db: duckdb.DuckDBPyConnection, metric: str, fields: dict[str, Any]
) -> None:
    with_rules, inline = lending(), inline_rules(lending())
    original_plan = plan_request(with_rules, request(metric, **fields))
    inline_plan = plan_request(inline, request(metric, **fields))
    uses_rule = [i.measure_rule for i in original_plan.inputs if i.measure_rule is not None] + [
        f for i in original_plan.inputs for f in i.definition_filters if hasattr(f, "expr")
    ]
    assert uses_rule, "the metric under test no longer uses a rule"
    assert not [i for i in inline_plan.inputs if i.measure_rule is not None]
    got = run_tree(lending_db, build_tree(with_rules, original_plan))
    want = run_tree(lending_db, build_tree(inline, inline_plan))
    assert got and same_rows(got, want)


def test_the_sample_model_keeps_its_rules_after_the_inline_copy_is_made() -> None:
    inline_rules(lending())
    assert lending().entities["contract_snapshot"].measures is not None
    measure = next(
        m for m in lending().entities["contract_snapshot"].measures or []
        if m.name == "npf_outstanding_principal"
    )  # fmt: skip
    assert measure.rule == "non_performing"


# --------------------------------------------------------------------------------------
# NULL join keys (section 7.5: joins are LEFT JOINs, so a missing key is a NULL group)


def small_contract_db() -> duckdb.DuckDBPyConnection:
    """Three disbursed contracts: one in each of two branches and one with no booking branch."""
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS lending")
    con.execute("CREATE SCHEMA lending.core")
    con.execute("USE lending.core")
    con.execute("CREATE TABLE branches(branch_id INTEGER, region_name VARCHAR)")
    con.execute("INSERT INTO branches VALUES (1, 'North'), (2, 'South')")
    con.execute(
        "CREATE TABLE contracts(contract_id INTEGER, booking_branch_id INTEGER, "
        "principal_amount DECIMAL(18,2), disbursement_date DATE)"
    )
    con.execute(
        "INSERT INTO contracts VALUES (1, 1, 100, DATE '2026-01-10'), "
        "(2, 2, 200, DATE '2026-01-11'), (3, NULL, 400, DATE '2026-01-12')"
    )
    return con


def test_every_join_is_a_left_join() -> None:
    for metric in ("collection_rate", "npf_ratio", "restructured_outstanding_principal"):
        tree = tree_of(lending(), metric, dimensions=["branch.region_name"])
        joins = list(tree.find_all(exp.Join))
        assert joins and {j.side for j in joins} == {"LEFT"}, metric


def test_a_row_with_no_join_key_is_kept_under_a_null_group() -> None:
    con = small_contract_db()
    simple = run_tree(
        con, tree_of(lending(), "disbursement_amount", dimensions=["branch.region_name"])
    )
    assert simple == [("North", 100), ("South", 200), (None, 400)]
    ratio = run_tree(
        con, tree_of(lending(), "average_ticket_size", dimensions=["branch.region_name"])
    )
    assert [(r[0], float(r[-1])) for r in ratio] == [
        ("North", 100.0),
        ("South", 200.0),
        (None, 400.0),
    ]


# --------------------------------------------------------------------------------------
# Defence in depth: a plan built without `plan_request` cannot smuggle in a non-finite number


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
@pytest.mark.parametrize("op", [">", "in"])
def test_generation_refuses_a_non_finite_number_in_a_hand_built_plan(value: float, op: str) -> None:
    model = lending()
    plan = plan_request(
        model,
        request(
            "disbursement_amount",
            filters=[{"dimension": "contract.annual_rate_pct", "op": ">", "value": 1}],
        ),
    )
    (flt,) = plan.filters
    bad = replace(flt, op=op, value=(1.0, value) if op == "in" else value)
    with pytest.raises(QueryError) as info:
        build_tree(model, replace(plan, filters=(bad,)))
    assert info.value.code == ErrorCode.INVALID_FILTER
