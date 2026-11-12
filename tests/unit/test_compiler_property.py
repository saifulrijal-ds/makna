"""WP-10 criterion 6: a seeded property test over the sample model.

`hypothesis` is not a dev dependency and this package adds none, so the cases come from a seeded
`random.Random`: the same 600 requests every run. Each request has a random metric, random
dimensions drawn from all dimensions of the model (personal ones, ones the metric cannot reach,
and misspelt ones included), random filters, and sometimes a grain and a time range. Every
request must either raise a section 7.7 error, or compile to SQL that passes
`governance.verify` and whose DuckDB result has:

1. no raw value of any personal column of the fixture `customers` table. A raw value that equals
   its own masked form (a birth date on 1 January, which `truncate_date` to the year leaves
   unchanged) reveals nothing beyond the mask and is left out of the comparison;
2. when a personal dimension is selected, no group that has fewer than `min_group_size` customers
   behind it. The compiler's own counts cannot be the reference, and an independent count for an
   arbitrary metric would mean rewriting the compiler, so the test checks a necessary condition: a
   group built from n customers needs at least n customers in the `customers` table with the
   same customer-level key values, so that count must reach the minimum. The exact counts are
   checked against reference queries in `test_compiler_group_size.py`.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any

import duckdb

from makna.compiler.compile import compile_plan
from makna.compiler.governance import GROUPS_LEFT_OUT, verify
from makna.compiler.plan import available_dimensions, plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import build_tree, reparse
from makna.model.definitions import Dimension
from makna.model.governance import GovernancePolicy
from makna.model.request import ErrorCode, QueryError, QueryRequest

from .compiler_support import lending
from .duckdb_support import run_sql, run_tree

CASES = 600
SEED = 20261112
PERSONAL_COLUMNS = ("full_name", "national_id", "phone_number", "email", "birth_date")


def raw_personal_values(con: duckdb.DuckDBPyConnection) -> set[str]:
    """Raw values of the personal columns of `customers`, less those that equal their mask."""
    found: set[str] = set()
    for column in PERSONAL_COLUMNS:
        for (value,) in run_sql(con, f"SELECT DISTINCT {column} FROM customers"):
            if value is None:
                continue
            if column == "birth_date" and (value.month, value.day) == (1, 1):
                continue  # equal to its own masked form
            found.add(str(value))
    return found


def customer_key_sql(model: Model, ref: str) -> str:
    """A customer dimension as written over the raw `customers` table (the masked form for the
    one personal dimension that can be selected), for the necessary-condition count."""
    if ref == "customer.birth_year":
        return "CAST(date_trunc('year', birth_date) AS DATE)"
    found = model.dimension(ref)
    assert found is not None
    return found[1].expr or found[1].name


def value_for(rng: random.Random, dimension: Dimension) -> Any:
    if dimension.values:
        return rng.choice(dimension.values).value
    if dimension.type == "numeric":
        return rng.choice([0, 1, 12, 36.5])
    if dimension.type == "time":
        return rng.choice(["2026-03-01", "2025-12-31"])
    if dimension.type == "boolean":
        return rng.choice([True, False])
    return rng.choice(["Jakarta", "x", "Java"])


def random_request(
    rng: random.Random, metrics: list[str], refs: list[str], personal: list[str]
) -> QueryRequest:
    model = lending()
    metric = rng.choice(metrics)
    offered = list(available_dimensions(model, model.metrics[metric]).refs())

    def pick() -> str:
        roll = rng.random()
        if roll < 0.15:
            return rng.choice(personal)
        if roll < 0.20:
            return "customer.no_such_dimension"
        return rng.choice(offered if roll < 0.85 and offered else refs)

    dimensions = [pick() for _ in range(rng.choice([0, 1, 1, 2, 2, 3]))]
    if rng.random() < 0.35:
        dimensions.append("customer.birth_year")
    filters = []
    for _ in range(rng.choice([0, 0, 1, 2])):
        ref = pick()
        found = model.dimension(ref)
        if found is None:
            continue
        dimension = found[1]
        op = rng.choice(["=", "!=", "in", "is_null"] if dimension.type != "numeric" else ["=", ">"])
        value: Any = None
        if op == "in":
            value = [value_for(rng, dimension), value_for(rng, dimension)]
        elif op != "is_null":
            value = value_for(rng, dimension)
        filters.append({"dimension": ref, "op": op, "value": value})
    fields: dict[str, Any] = {"metric": metric, "dimensions": dimensions, "filters": filters}
    if rng.random() < 0.3:
        fields["grain"] = rng.choice(["month", "quarter", "year"])
    if rng.random() < 0.4:
        fields["time_start"] = rng.choice(["2025-01-01", "2026-01-01"])
        fields["time_end"] = rng.choice(["2026-07-01", "2026-12-31"])
    return QueryRequest.model_validate(fields)


def test_random_requests_never_leak_personal_data_or_small_groups(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    model = lending()
    rng = random.Random(SEED)
    metrics = sorted(model.metrics)
    refs = [f"{e.name}.{d.name}" for e in model.entities.values() for d in e.dimensions]
    personal = [
        f"{e.name}.{d.name}"
        for e in model.entities.values()
        for d in e.dimensions
        if model.effective_sensitivity(e, d) == "personal"
    ]
    raw = raw_personal_values(lending_db)
    assert len(raw) > 500  # names, ids, phone numbers, emails, birth dates

    outcomes: Counter[str] = Counter()
    for _ in range(CASES):
        req = random_request(rng, metrics, refs, personal)
        minimum = rng.choice([2, 3, 5, 10])
        try:
            plan = plan_request(model, req, governance=GovernancePolicy.strict(minimum))
        except QueryError as error:
            assert isinstance(error.code, ErrorCode)
            outcomes[f"error {error.code}"] += 1
            continue
        tree = build_tree(model, plan)
        verify(tree, plan)  # compile_plan runs it again
        reparse(compile_plan(model, plan, "postgres").sql, "postgres")
        rows = run_tree(lending_db, tree)
        outcomes["compiled"] += 1

        leaked = {str(cell) for row in rows for cell in row} & raw
        assert not leaked, f"raw personal values in the result of {req}: {sorted(leaked)[:3]}"

        if not plan.guard:
            assert GROUPS_LEFT_OUT not in [c.name for c in plan.columns]
            continue
        outcomes["compiled with a personal dimension"] += 1
        names = [c.name for c in plan.columns]
        assert names[-1] == GROUPS_LEFT_OUT
        assert len({row[-1] for row in rows}) <= 1
        customer_keys = [
            (d.ref, names.index(d.column)) for d in plan.dimensions if d.entity == "customer"
        ]
        if not customer_keys:
            continue
        condition = " AND ".join(
            f"{customer_key_sql(model, ref)} IS NOT DISTINCT FROM ?" for ref, _ in customer_keys
        )
        for row in rows:
            key = [row[i] for _, i in customer_keys]
            (count,) = lending_db.execute(
                f"SELECT COUNT(*) FROM customers WHERE {condition}", key
            ).fetchone() or (0,)
            assert count >= minimum, f"a group of {count} customers in the result of {req}"
            outcomes["groups checked against the customers table"] += 1

    errors = sum(n for k, n in outcomes.items() if k.startswith("error"))
    assert outcomes["compiled"] + errors == CASES
    assert outcomes["compiled"] >= 250, outcomes
    assert outcomes["compiled with a personal dimension"] >= 60, outcomes
    assert outcomes[f"error {ErrorCode.PERSONAL_DATA_REFUSED}"] >= 20, outcomes
    assert errors >= 50, outcomes
    assert outcomes["groups checked against the customers table"] >= 200, outcomes
