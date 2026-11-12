"""WP-10B: a seeded property test over random governance policies.

The 600 requests of the WP-10 property test (same generator, a different seed) are compiled under
a random policy: `strict` with a random minimum, `custom` with random rules, or `off`. For each:

1. a request that `strict` plans is planned by every policy (relaxing never adds a refusal);
2. the SQL passes `verify` and re-parses for PostgreSQL;
3. the DuckDB result holds no raw value of a personal column that the policy does not let out
   (columns of dimensions with no mask, unless `allow_unmasked_personal` or `off`; the birth
   date, unless `apply_masks` is false or `off`);
4. with a minimum group size and a customer dimension selected, no group has fewer customers
   behind it than the minimum (the necessary condition of the WP-10 test);
5. the response has one warning that starts with "Governance mode" exactly when the policy
   relaxes a rule, and no `__groups_left_out` column when the minimum is 0.
"""

from __future__ import annotations

import random
from collections import Counter

import duckdb

from makna.compiler.compile import compile_plan
from makna.compiler.governance import GROUPS_LEFT_OUT, verify
from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import build_tree, reparse
from makna.model.governance import STRICT, GovernancePolicy
from makna.model.request import ErrorCode, QueryError

from .compiler_support import lending
from .duckdb_support import run_sql, run_tree
from .test_compiler_property import PERSONAL_COLUMNS, random_request

CASES = 600
SEED = 20261113
UNMASKED_COLUMNS = ("full_name", "national_id", "phone_number", "email")


def random_policy(rng: random.Random) -> GovernancePolicy:
    roll = rng.random()
    if roll < 0.15:
        return GovernancePolicy.off()
    if roll < 0.35:
        return GovernancePolicy.strict(rng.choice([2, 5, 10]))
    return GovernancePolicy.custom(
        min_group_size=rng.choice([0, 2, 5, 10]),
        allow_personal_filters=rng.random() < 0.5,
        apply_masks=rng.random() < 0.5,
        allow_unmasked_personal=rng.random() < 0.5,
    )


def leaked_values(con: duckdb.DuckDBPyConnection, policy: GovernancePolicy) -> set[str]:
    """Raw values of the personal columns that the policy does not let out."""
    columns: list[str] = []
    if not policy.allow_unmasked_personal:
        columns.extend(UNMASKED_COLUMNS)
    if policy.apply_masks:
        columns.append("birth_date")
    found: set[str] = set()
    for column in columns:
        assert column in PERSONAL_COLUMNS
        for (value,) in run_sql(con, f"SELECT DISTINCT {column} FROM customers"):
            if value is None:
                continue
            if column == "birth_date" and (value.month, value.day) == (1, 1):
                continue  # equal to its own masked form
            found.add(str(value))
    return found


def customer_key_sql(model: Model, ref: str, masked: bool) -> str:
    if masked and ref == "customer.birth_year":
        return "CAST(date_trunc('year', birth_date) AS DATE)"
    found = model.dimension(ref)
    assert found is not None
    return found[1].expr or found[1].name


def test_random_requests_under_random_policies(lending_db: duckdb.DuckDBPyConnection) -> None:
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
    outcomes: Counter[str] = Counter()
    for _ in range(CASES):
        req = random_request(rng, metrics, refs, personal)
        policy = random_policy(rng)
        strict_ok = True
        try:
            plan_request(model, req, governance=STRICT)
        except QueryError:
            strict_ok = False
        try:
            plan = plan_request(model, req, governance=policy)
        except QueryError as error:
            assert isinstance(error.code, ErrorCode)
            assert not strict_ok, f"{policy.mode} refused what strict plans: {req}"
            outcomes[f"error {policy.mode}"] += 1
            continue
        outcomes[f"compiled {policy.mode}"] += 1
        outcomes["strict also plans" if strict_ok else "only the relaxed policy plans"] += 1

        tree = build_tree(model, plan)
        verify(tree, plan)
        response = compile_plan(model, plan, "postgres")
        reparse(response.sql, "postgres")
        rows = run_tree(lending_db, tree)

        names = [c.name for c in plan.columns]
        warnings = [w for w in response.warnings if w.startswith("Governance mode")]
        assert len(warnings) == (1 if policy.relaxed() else 0)
        if policy.min_group_size == 0:
            assert GROUPS_LEFT_OUT not in names
            assert "__records_" not in response.sql

        leaked = {str(cell) for row in rows for cell in row} & leaked_values(lending_db, policy)
        assert not leaked, f"{policy} leaked {sorted(leaked)[:3]} in the result of {req}"

        raw_dims = [d for d in plan.dimensions if d.personal and not d.masked]
        if raw_dims:
            outcomes["a personal dimension returned raw"] += 1
        if not plan.guard:
            continue
        assert names[-1] == GROUPS_LEFT_OUT
        outcomes["compiled with the minimum group size"] += 1
        customer_keys = [
            (d, names.index(d.column)) for d in plan.dimensions if d.entity == "customer"
        ]
        if not customer_keys:
            continue
        condition = " AND ".join(
            f"{customer_key_sql(model, d.ref, d.masked)} IS NOT DISTINCT FROM ?"
            for d, _ in customer_keys
        )
        for row in rows:
            key = [row[i] for _, i in customer_keys]
            (count,) = lending_db.execute(
                f"SELECT COUNT(*) FROM customers WHERE {condition}", key
            ).fetchone() or (0,)
            assert count >= policy.min_group_size, f"a group of {count} in {req} under {policy}"
            outcomes["groups checked"] += 1

    assert outcomes["compiled off"] >= 20, outcomes
    assert outcomes["compiled custom"] >= 150, outcomes
    assert outcomes["compiled strict"] >= 40, outcomes
    assert outcomes["only the relaxed policy plans"] >= 20, outcomes
    assert outcomes["a personal dimension returned raw"] >= 25, outcomes
    assert outcomes["compiled with the minimum group size"] >= 30, outcomes
    assert outcomes["groups checked"] >= 100, outcomes
