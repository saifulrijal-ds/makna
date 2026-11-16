"""Privacy probe (findings C-4, open questions 2 and 6), on the synthetic sample data only.

(a) Six customer dimensions that are not personal, selected together: how many result rows are
    one customer. No personal dimension is selected, so no minimum group size applies.
(b) Differencing: two requests by the masked `customer.birth_year` that differ by one customer
    (a registration date on which exactly one customer registered) reveal that customer's
    birth year.

Needs the settings of `python -m evals compile` (application database, `MAKNA_ENGINE_URL`).
Run from the repository root:

    uv run python docs/reviews/core-complete-probes/privacy.py
"""

from typing import Any

from evals.runtime import open_runtime, require_engine_url
from evals.sqlrun import SqlRunner
from makna.model.request import QueryRequest
from makna.services import compile_on_snapshot
from makna.services.snapshot import SnapshotCache

rt = open_runtime()
snapshot, _ = SnapshotCache(rt.engine).current()


def sql_of(**request: Any) -> str:
    parsed = QueryRequest.model_validate(request)
    return compile_on_snapshot(snapshot, parsed, policy=rt.policy, dialect="postgres").sql


with SqlRunner(require_engine_url(rt), rt.statement_timeout_seconds) as runner:
    quasi = [
        "customer.city",
        "customer.occupation_group",
        "customer.gender",
        "customer.income_band",
        "customer.registered_date",
        "customer.customer_segment",
    ]
    rows = runner.run(sql_of(metric="new_customer_count", dimensions=quasi, limit=10000)).rows
    customers = runner.run('SELECT COUNT(*) FROM "lending"."core"."customers"').rows[0][0]
    alone = sum(1 for row in rows if row[-1] == 1)
    print(f"(a) {len(rows)} groups for {customers} customers; {alone} groups hold one customer")

    day = runner.run(
        'SELECT CAST(registered_at AS DATE) FROM "lending"."core"."customers" '
        "GROUP BY 1 HAVING COUNT(*) = 1 ORDER BY 1 LIMIT 1"
    ).rows[0][0]
    by_year = ["customer.birth_year"]
    without = [{"dimension": "customer.registered_date", "op": "!=", "value": str(day)}]
    first = dict(
        (r[0], r[1]) for r in runner.run(sql_of(metric="new_customer_count", dimensions=by_year)).rows
    )
    second = dict(
        (r[0], r[1])
        for r in runner.run(
            sql_of(metric="new_customer_count", dimensions=by_year, filters=without)
        ).rows
    )
    changed = [(year, first[year], second.get(year)) for year in first if first[year] != second.get(year)]
    print(f"(b) the one customer registered on {day}; birth-year rows that differ: {changed}")
