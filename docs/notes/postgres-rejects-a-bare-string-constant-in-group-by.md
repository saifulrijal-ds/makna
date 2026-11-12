PostgreSQL rejects `GROUP BY 'abc'` ("non-integer constant in GROUP BY"); a masked constant must be a typed expression such as `CAST('***' AS TEXT)`.

Checked on 2026-11-12 (PostgreSQL 16.2): `SELECT 'a', count(*) FROM t GROUP BY 'a'` fails, `GROUP BY CAST('***' AS TEXT)` works, and so does grouping by a CTE column that holds the cast. DuckDB accepts both, so a DuckDB run cannot show the problem.

- `redact` in `makna.compiler.governance.mask_expression` returns the typed constant for this reason, and a test pins the printed text.
- Leaving the constant out of the `GROUP BY` instead would change the result of an empty input: an aggregate without `GROUP BY` returns one row, with `GROUP BY` none.
