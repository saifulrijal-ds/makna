"""WP-10 criterion 7 and the unchanged-SQL guarantee: the minimum group size.

A request that selects a personal dimension gets groups built from fewer than `min_group_size`
records of the entity holding that dimension left out. Every input counts those records, so a
ratio drops a group when either input has it from too few. The count of groups left out cannot be
known when compiling (Makna runs no caller SQL, D-21): the generated SQL adds the column
`__groups_left_out` and the warning names it.

The reference counts below come from hand-written queries over the raw tables, not from the
compiler.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from makna.compiler.compile import compile_request
from makna.compiler.governance import GROUPS_LEFT_OUT
from makna.compiler.plan import DEFAULT_MIN_GROUP_SIZE, plan_request
from makna.compiler.sqlgen import build_tree

from .compiler_support import lending, request
from .duckdb_support import run_sql, run_tree

GOLDEN = Path(__file__).parent / "golden" / "compiler_sql_before_wp10.json"
PERSONAL = ["customer.birth_year", "customer.province", "branch.region_name"]
LATEST = "(SELECT MAX(snapshot_date) FROM contract_monthly_snapshots)"
BIRTH_YEAR = "CAST(date_trunc('year', cu.birth_date) AS DATE)"


def groups(con: duckdb.DuckDBPyConnection, metric: str, min_size: int, **fields: Any) -> Any:
    """Run a request and return (key tuples with values, the `__groups_left_out` column)."""
    plan = plan_request(lending(), request(metric, **fields), min_group_size=min_size)
    rows = run_tree(con, build_tree(lending(), plan))
    names = [c.name for c in plan.columns]
    return plan, names, rows


# ---- requests without a personal dimension are unchanged -------------------------------


@pytest.mark.parametrize("case", sorted(json.loads(GOLDEN.read_text())))
def test_a_request_without_a_personal_dimension_compiles_to_the_same_sql_as_before(
    case: str,
) -> None:
    """The golden file holds the PostgreSQL text the compiler produced before WP-10."""
    entry = json.loads(GOLDEN.read_text())[case]
    response = compile_request(lending(), request(entry["metric"], **entry["fields"]))
    assert response.sql == entry["sql"]
    assert GROUPS_LEFT_OUT not in [c.name for c in response.columns]
    assert not any("minimum" in w or "left out" in w for w in response.warnings)


def test_the_golden_cases_cover_simple_ratio_and_balance_metrics() -> None:
    names = {entry["metric"] for entry in json.loads(GOLDEN.read_text()).values()}
    assert {"disbursement_amount", "npf_ratio", "outstanding_principal"} <= names


# ---- the parameter ---------------------------------------------------------------------


def test_the_default_minimum_is_ten_like_the_configuration() -> None:
    plan = plan_request(lending(), request("new_customer_count"))
    assert plan.min_group_size == DEFAULT_MIN_GROUP_SIZE == 10


@pytest.mark.parametrize("bad", [0, -1])
def test_a_minimum_below_one_is_rejected(bad: int) -> None:
    with pytest.raises(ValueError, match="min_group_size"):
        plan_request(lending(), request("new_customer_count"), min_group_size=bad)
    with pytest.raises(ValueError, match="min_group_size"):
        compile_request(lending(), request("new_customer_count"), min_group_size=bad)


def test_the_minimum_goes_into_the_sql_and_the_warning() -> None:
    response = compile_request(
        lending(), request("new_customer_count", dimensions=["customer.birth_year"]),
        min_group_size=7,
    )  # fmt: skip
    assert ">= 7" in response.sql
    [warning] = [w for w in response.warnings if GROUPS_LEFT_OUT in w]
    assert "7" in warning
    assert "Customer" in warning
    assert "personal" in warning
    assert "no rows" in warning  # an empty result can mean every group was left out


# ---- the result column -----------------------------------------------------------------


def test_the_count_of_groups_left_out_is_the_last_column_and_the_same_on_every_row(
    lending_full_db: duckdb.DuckDBPyConnection,
) -> None:
    plan, names, rows = groups(
        lending_full_db, "outstanding_principal", 20, dimensions=["customer.birth_year"]
    )
    assert names[-1] == GROUPS_LEFT_OUT
    assert plan.columns[-1].role == "governance"
    assert len(rows) > 3
    assert len({row[-1] for row in rows}) == 1
    assert rows[0][-1] > 0


def test_the_column_name_cannot_clash_with_a_dimension_or_metric_column() -> None:
    assert GROUPS_LEFT_OUT.startswith("__")
    names = {m for m in lending().metrics}
    names |= {f"{e.name}__{d.name}" for e in lending().entities.values() for d in e.dimensions}
    assert GROUPS_LEFT_OUT not in names


def test_the_number_left_out_ignores_the_limit(lending_full_db: duckdb.DuckDBPyConnection) -> None:
    kwargs: dict[str, Any] = {"dimensions": ["customer.birth_year", "customer.province"]}
    _, _, everything = groups(lending_full_db, "outstanding_principal", 20, **kwargs)
    _, _, one = groups(lending_full_db, "outstanding_principal", 20, limit=1, **kwargs)
    assert len(one) == 1
    assert one[0][-1] == everything[0][-1]


def test_the_record_counts_are_never_returned() -> None:
    response = compile_request(lending(), request("npf_ratio", dimensions=["customer.birth_year"]))
    assert [c.name for c in response.columns] == [
        "customer__birth_year", "npf_outstanding_principal", "outstanding_principal", "npf_ratio",
        GROUPS_LEFT_OUT,
    ]  # fmt: skip


def test_the_sql_for_a_personal_request_parses_for_postgres() -> None:
    import sqlglot

    for metric in ("new_customer_count", "npf_ratio", "collection_rate"):
        sql = compile_request(lending(), request(metric, dimensions=["customer.birth_year"])).sql
        sqlglot.parse_one(sql, read="postgres")


# ---- criterion 7: against reference counts ---------------------------------------------

NPF_GROUPS = f"""
SELECT {BIRTH_YEAR} AS y, cu.province AS p, b.region_name AS r,
       COUNT(DISTINCT cu.customer_id) AS n, SUM(s.outstanding_principal) AS v
FROM contract_monthly_snapshots s
JOIN contracts c ON c.contract_id = s.contract_id
JOIN customers cu ON cu.customer_id = c.customer_id
LEFT JOIN branches b ON b.branch_id = c.servicing_branch_id
WHERE s.days_past_due > 90 AND s.snapshot_date = {LATEST}
GROUP BY 1, 2, 3
"""


def keyed(rows: list[tuple[Any, ...]], width: int) -> dict[tuple[Any, ...], tuple[Any, ...]]:
    return {tuple(r[:width]): tuple(r[width:]) for r in rows}


@pytest.mark.parametrize("minimum", [2, 3])
def test_npf_by_birth_year_province_and_region_leaves_out_small_groups(
    lending_full_db: duckdb.DuckDBPyConnection, minimum: int
) -> None:
    reference = run_sql(lending_full_db, NPF_GROUPS)
    plan, _, rows = groups(
        lending_full_db, "npf_outstanding_principal", minimum, dimensions=PERSONAL
    )
    kept = {(y, p, r): (n, v) for y, p, r, n, v in reference if n >= minimum}
    dropped = [key for y, p, r, n, _ in reference if n < minimum for key in [(y, p, r)]]
    assert kept and dropped, "the data must have groups on both sides of the minimum"

    got = keyed(rows, 3)
    assert set(got) == set(kept)  # exactly the large groups, no small one
    for key, (value, left_out) in got.items():
        assert float(value) == pytest.approx(float(kept[key][1]))
        assert left_out == len(dropped)
    assert plan.min_group_size == minimum


def test_with_the_default_minimum_nothing_is_left_when_every_group_is_small(
    lending_full_db: duckdb.DuckDBPyConnection,
) -> None:
    reference = run_sql(lending_full_db, NPF_GROUPS)
    assert max(r[3] for r in reference) < DEFAULT_MIN_GROUP_SIZE
    _, _, rows = groups(lending_full_db, "npf_outstanding_principal", 10, dimensions=PERSONAL)
    assert rows == []  # every group was left out; the warning says an empty result can mean that


def test_the_same_metric_by_region_alone_is_not_suppressed(
    lending_full_db: duckdb.DuckDBPyConnection,
) -> None:
    plan, names, rows = groups(
        lending_full_db, "npf_outstanding_principal", 10, dimensions=["branch.region_name"]
    )
    reference = run_sql(
        lending_full_db,
        f"""SELECT b.region_name, SUM(s.outstanding_principal)
        FROM contract_monthly_snapshots s JOIN contracts c ON c.contract_id = s.contract_id
        LEFT JOIN branches b ON b.branch_id = c.servicing_branch_id
        WHERE s.days_past_due > 90 AND s.snapshot_date = {LATEST} GROUP BY 1 ORDER BY 1""",
    )
    assert GROUPS_LEFT_OUT not in names
    assert [r[0] for r in rows] == [r[0] for r in reference]
    assert len(rows) > 1


def test_a_request_with_a_personal_dimension_and_a_filter_counts_only_the_filtered_records(
    lending_full_db: duckdb.DuckDBPyConnection,
) -> None:
    flt = {"dimension": "customer.gender", "op": "=", "value": "F"}
    _, _, rows = groups(
        lending_full_db,
        "new_customer_count",
        55,
        dimensions=["customer.birth_year"],
        filters=[flt],
    )
    reference = run_sql(
        lending_full_db,
        f"""SELECT {BIRTH_YEAR}, COUNT(*) FROM customers cu WHERE cu.gender = 'F' GROUP BY 1""",
    )
    want = {y: n for y, n in reference if n >= 55}
    assert {r[0]: r[1] for r in rows} == want
    assert want and len(want) < len(reference)


# ---- a ratio: either input with too few records drops the group ------------------------

INPUT_GROUPS = """
SELECT {key} AS y, cu.province AS p,
       COUNT(DISTINCT cu.customer_id) AS n, SUM(s.outstanding_principal) AS v
FROM contract_monthly_snapshots s
JOIN contracts c ON c.contract_id = s.contract_id
JOIN customers cu ON cu.customer_id = c.customer_id
WHERE s.snapshot_date = {latest} {extra}
GROUP BY 1, 2
"""


def test_a_ratio_drops_a_group_when_either_input_has_too_few_records(
    lending_full_db: duckdb.DuckDBPyConnection,
) -> None:
    minimum = 3
    num = keyed(
        run_sql(
            lending_full_db,
            INPUT_GROUPS.format(key=BIRTH_YEAR, latest=LATEST, extra="AND s.days_past_due > 90"),
        ),
        2,
    )
    den = keyed(
        run_sql(lending_full_db, INPUT_GROUPS.format(key=BIRTH_YEAR, latest=LATEST, extra="")), 2
    )

    def kept(key: tuple[Any, ...]) -> bool:
        # An input with no rows for the group does not drop it; one with few rows does.
        return all(side[key][0] >= minimum for side in (num, den) if key in side)

    wanted = {key for key in set(num) | set(den) if kept(key)}
    only_denominator = {key for key in wanted if key not in num}
    small_numerator = {
        key for key in den if den[key][0] >= minimum and key in num and not kept(key)
    }
    assert only_denominator, "the data must have a group with no non-performing rows"
    assert small_numerator, "the data must have a group the numerator alone would drop"

    plan = plan_request(
        lending(),
        request("npf_ratio", dimensions=["customer.birth_year", "customer.province"]),
        min_group_size=minimum,
    )
    rows = run_tree(lending_full_db, build_tree(lending(), plan))
    assert {tuple(r[:2]) for r in rows} == wanted
    for row in rows:
        key = tuple(row[:2])
        if key in only_denominator:
            assert row[2] == 0 or row[2] is None  # numerator: no rows, shown as zero
            assert float(row[4]) == 0.0


# ---- several entities, and a composite key ----------------------------------------------


def redacting(*refs: str) -> Any:
    """The sample model with the given categorical dimensions personal and redacted."""
    from makna.model.definitions import MaskRedact

    from .test_compiler_masking import dimension_of, masked

    model = lending()
    for ref in refs:
        mask = dimension_of(model, ref, masking=MaskRedact(method="redact"), sensitivity="personal")
        model = masked(model, ref, mask)
    return model


def test_each_entity_with_a_personal_dimension_is_counted_and_all_must_be_large_enough(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    model = redacting("branch.region_name")
    fields = {"dimensions": ["customer.birth_year", "branch.region_name"]}
    plan = plan_request(model, request("outstanding_principal", **fields), min_group_size=2)
    assert [g.entity for g in plan.guard] == ["customer", "branch"]
    rows = run_tree(lending_db, build_tree(model, plan))
    response = compile_request(model, request("outstanding_principal", **fields), min_group_size=2)
    assert response.sql.count("COUNT(DISTINCT") == 2
    assert all(row[1] == "***" for row in rows)
    assert [c.name for c in plan.columns][-1] == GROUPS_LEFT_OUT
    # A branch is a group of one when only one branch is behind the group: with the minimum
    # above the branch count nothing is left, and every group is counted as left out.
    plan = plan_request(model, request("outstanding_principal", **fields), min_group_size=10**6)
    assert run_tree(lending_db, build_tree(model, plan)) == []


def test_an_entity_with_a_composite_key_is_counted_by_the_whole_key(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    model = redacting("contract_snapshot.collectibility")
    plan = plan_request(
        model,
        request("outstanding_principal", dimensions=["contract_snapshot.collectibility"]),
        min_group_size=1,
    )
    assert plan.guard[0].primary_key == ("snapshot_date", "contract_id")
    rows = run_tree(lending_db, build_tree(model, plan))
    assert [row[0] for row in rows] == ["***"]
    assert rows[0][-1] == 0
