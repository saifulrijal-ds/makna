`a / b` on two integers truncates on PostgreSQL and gives a fraction on DuckDB; the compiler must not rely on the engine for the result type of a division.

Checked on 2026-11-11 (PostgreSQL 16.2, DuckDB 2.0.0, sqlglot 30.31.0): `SELECT 1/2` returns 0 on PostgreSQL and 0.5 on DuckDB. A unit test that runs a derived metric on DuckDB therefore cannot prove the same SQL divides correctly on PostgreSQL.

- A ratio always casts its numerator: `CAST(n AS DOUBLE) / NULLIF(d, 0)`.
- A derived metric's `a / b` is parsed by sqlglot, which prints it for `postgres` as `CAST(a AS DOUBLE PRECISION) / b`. That is sqlglot behaviour, not Makna code, so `tests/unit/test_compiler_sqlgen.py` asserts the cast in the printed PostgreSQL text. If a sqlglot upgrade removes it, that test fails and the compiler has to add the cast itself.
- Compare the printed SQL, not only the DuckDB result, whenever a type rule differs between the engines.
- The `bucket` mask (WP-10) is `FLOOR(CAST(value AS DOUBLE PRECISION) / width) * width`. Without the cast, an integer column on PostgreSQL would truncate toward zero (`-3 / 10` is `0`, not the band starting at `-10`), and DuckDB would still pass. A test pins the cast in the printed PostgreSQL text and runs the expression on negative values.
