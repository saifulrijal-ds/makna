V05 compares the PostgreSQL text of the neutral parse with that of a PostgreSQL parse, and two spellings that mean the same are normalised before the comparison.

Definition expressions are read with sqlglot's default parser (`sqlglot.parse_one(sql, into=exp.Condition)`) and printed for `postgres`, while a curator writes PostgreSQL (D-32 ruling 2, C-5). The two parses disagree on `^` (an exclusive or against a power), on `int2`/`int4`/`int8` (the default parser reads `int8` as a one-byte integer, and prints `SMALLINT`), on `CONCAT(a, b)` (printed as `a || b` against `CONCAT(a, b)`, which treat NULL differently), on `DATE_PART('year', d)` and `BTRIM(x)` (printed differently, same meaning). V05 refuses every expression whose PostgreSQL print differs, and says what to write.

Two differences are not disagreements and are cleared on both trees before the comparison (`makna.definitions.validate._same_meaning_form`):

- Division: a PostgreSQL parse marks `a / b` as typed, the neutral parse does not, so only the neutral print adds the floating-point cast (see `integer-division-differs-between-postgres-and-duckdb.md`).
- `x IS NOT NULL`: a PostgreSQL parse gives one `Is` node with `negate=True`, the neutral parse gives `Not(Is(...))`, and they print as `x IS NOT NULL` and `NOT x IS NULL`. The sample definitions write `IS NOT NULL`, so a check without this would refuse them.

The raw-text and type-name checks run on the text: the tokenizer keeps the spelling `int8` that the parser loses (`CAST(x AS "int8")` reads as a type too), and the printers can raise on a node they do not support (`DATETIME(x)` raises `AttributeError` for DuckDB), which is a V05 issue and not a crash. When another sqlglot upgrade changes what prints differently, the test table in `tests/unit/test_expression_dialect.py` shows which expressions stay valid.
