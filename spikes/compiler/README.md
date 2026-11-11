# Spike: compile metrics to Trino SQL with sqlglot

**Question.** Is the schema in `semantic/schema/` enough to compile the hard
cases into correct Trino SQL, using sqlglot only (P-3)?

**How to run** (about 10 seconds, no services needed):

```
uv run --with sqlglot --with duckdb --with pyyaml python spikes/compiler/run_spike.py
```

Tested with sqlglot 30.31.0 and DuckDB 2.0.0 on 2026-11-11. Captured output: `output.txt`.

**What it does.** `mini_compiler.py` loads the example definitions from
`semantic/schema/examples/`, compiles a request into a sqlglot syntax tree, and
prints Trino SQL. `run_spike.py` builds a small seeded lending dataset in
DuckDB (database `lending`, schema `core`, so the three-part Trino names
resolve unchanged), executes the **exact Trino SQL text** on DuckDB, and
compares every row with a reference query written by hand without the compiler.

**What it proved.**

1. A simple metric with a pinned join (`disbursement_amount` by booking-branch region, by month) matches the reference.
2. A ratio of two balance measures (`npf_ratio`) through a two-hop join (snapshot to contract to servicing branch) at quarter grain uses only the last month end of each quarter and matches the reference.
3. A filter on a dimension two joins away, with no grain, uses only the latest snapshot and matches.
4. A ratio whose two sides live in different fact tables (`collection_rate` = payments / installment schedules), with two dimensions reached by multi-hop joins, matches a hand-written `FULL JOIN` reference. The same question answered with a naive row-to-row join gives 1.0006 instead of 0.9411: this is the fan-out error the compiler design prevents.
5. A balance over a multi-month range is not summed across months.
6. Governance: masked personal dimensions come out masked; an unmasked personal dimension, and any filter on personal data, are refused; a grain the metric does not allow is refused; a filter value containing SQL stays a string literal; two equally short join paths with no pin are refused with the options listed.

The spike's request uses the short field names `start` and `end`; the product request uses `time_start` and `time_end` (`docs/ARCHITECTURE.md` section 7.1).

**Design points the product compiler should keep.**

- Every simple metric is aggregated in its own subquery (CTE) from its own base table, joining only "many-to-one" relationships. Nothing is ever joined fact to fact.
- Inputs of a ratio or derived metric are combined with `UNION ALL` followed by `GROUP BY` on the shared keys, not with a `FULL OUTER JOIN`. Grouping treats NULL keys as equal, so no null-safe join condition is needed, and it extends to any number of inputs.
- Expressions from YAML are parsed with sqlglot and their columns are bound to a table alias. Filter values become literals in the syntax tree; no SQL text is ever concatenated.
- A final pass walks the syntax tree and fails if a personal column appears outside a mask or an aggregate.

**What is not proven.**

- The SQL was not executed on a Trino server (no Docker or Trino download in the design environment). It re-parses with sqlglot's Trino dialect and uses only `DATE_TRUNC`, `CAST`, `NULLIF`, `COALESCE`, `UNION ALL`, CTEs, `IN (subquery)`, and quoted identifiers. The first build package that brings up Trino (WP-11) must run these same five cases on Trino.
- sqlglot prints the unit as `'MONTH'` in upper case. Trino is expected to accept it; confirm in WP-11.
- `SUBSTRING`-based partial masking was executed on DuckDB only. Partial masking was removed from the design after the review (`docs/reviews/design.md`, R-4): grouping by the last four phone digits returned one row per customer. The governance checks and `output.txt` were updated to match; the masked-dimension check now uses `customer.birth_year` only, and the phone number is refused.
- The minimum group size rule (`docs/ARCHITECTURE.md` section 7.6) was added after the spike and is not in it.
- Performance on large tables was not measured.
