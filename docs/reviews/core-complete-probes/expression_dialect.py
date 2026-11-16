"""Expression dialect probe (finding C-5, open question 9): how a definition expression written
as PostgreSQL is printed for PostgreSQL after the default-dialect parse the product uses, next to
the print after a PostgreSQL parse. Needs nothing but the Python environment:

    uv run python docs/reviews/core-complete-probes/expression_dialect.py
"""

import sqlglot
from sqlglot import exp

from makna.model.functions import first_disallowed_function

CASES = [
    "x::int8", "CAST(x AS int8)", "x::int4", "x::int2", "x::float8", "x::bigint",
    "2 ^ 3", "a / b", "a % b", "x ~ 'abc'", "x ILIKE 'a%'", "a || b",
    "x::date - y::date", "EXTRACT(dow FROM d)", "DATE_PART('year', d)", "d + INTERVAL '1 month'",
    "TO_CHAR(d, 'YYYY-MM')", "ROUND(x / 3, 2)", "x::numeric(10,2)", "FLOOR(x / 10) * 10",
    "NOW()", "CURRENT_DATE", "TRUNC(x)", "LOG(x)",
]  # fmt: skip

for case in CASES:
    try:
        default = sqlglot.parse_one(case, into=exp.Condition)
        postgres = sqlglot.parse_one(case, into=exp.Condition, read="postgres")
    except sqlglot.errors.SqlglotError as error:
        print(f"{case:28s} -> does not parse ({type(error).__name__})")
        continue
    printed, wanted = default.sql("postgres"), postgres.sql("postgres")
    allowed = first_disallowed_function(default) or "allowed"
    note = "" if printed == wanted else f"   DIFFERS from a PostgreSQL parse: {wanted}"
    print(f"{case:28s} -> {printed:48s} [{allowed}]{note}")
