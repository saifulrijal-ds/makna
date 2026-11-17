"""The functions a definition expression may call, and a check for them (V05, the final check).

A dimension, measure, or rule expression is SQL written by a curator and run by the engine. A
function that reads or writes session state (`set_config`, `current_setting`), reads the system
(`pg_read_file`), or is volatile (`random`, `nextval`) can carry a personal value from one place
to another where no check on columns can see it. A deny-list always misses a name (docs/notes/
postgres-reads-a-missing-column-as-a-function-of-the-row.md), so the rule is an allow-list: a
function is accepted only if it is listed here.

The list holds ordinary, pure scalar functions: conditionals and casts, arithmetic and rounding,
text, and date handling. Aggregates and windows are refused by V05 for another reason and are not
on it. Operators (`+`, `||`, `IN`, `BETWEEN`, `IS NULL`) are not functions and need no entry.

To add a function: add its sqlglot node class to `ALLOWED_NODES` (or, for a name sqlglot does not
know and parses as `Anonymous`, its lower-case name to `ALLOWED_ANONYMOUS`), with a test, and say
in the pull request why it is pure and cannot read state. No setting or definition can extend the
list.
"""

from __future__ import annotations

from sqlglot import exp

_NODE_NAMES = (
    # conditionals, casts, and the boolean connectors sqlglot models as functions
    "Cast", "Case", "If", "Coalesce", "Nullif", "And", "Or", "Xor", "Not",
    # arithmetic and rounding
    "Abs", "Floor", "Ceil", "Sign", "Sqrt", "Pow", "Mod",
    # text
    "Lower", "Upper", "Initcap", "Trim", "Substring", "Length", "Concat", "ConcatWs",
    "Left", "Right", "Replace", "StrPosition", "SplitPart", "Pad",
    # dates: truncation, extraction, differences
    "TimestampTrunc", "DateTrunc", "Extract", "Year", "Month", "Day",
    "DateAdd", "DateSub", "TsOrDsToDate", "TsOrDsAdd", "ToChar",
)  # fmt: skip
ALLOWED_NODES: tuple[type[exp.Expr], ...] = tuple(
    node for name in _NODE_NAMES if isinstance(node := getattr(exp, name, None), type)
)
ALLOWED_ANONYMOUS: frozenset[str] = frozenset({"btrim", "ltrim", "rtrim", "date_part"})


# Types left out on purpose, because PostgreSQL and DuckDB give different values: `CHAR` (PostgreSQL
# pads and truncates to the length, DuckDB does not), `TINYINT` (sqlglot prints it as SMALLINT, so a
# value of 300 does not overflow on PostgreSQL), and `TIMESTAMPTZ` (a cast that DuckDB rejects).
# Types a cast may target. Everything else is refused, `reg*` types and user-defined types
# included: casting a personal value to a type that can reject it (`CAST(full_name AS INTEGER)`,
# `CAST(national_id AS regclass)`) makes the engine print the value in its error message.
ALLOWED_CAST_TYPES: frozenset[object] = frozenset(
    getattr(exp.DataType.Type, name)
    for name in (
        "SMALLINT", "INT", "BIGINT", "DECIMAL", "FLOAT", "DOUBLE",
        "TEXT", "VARCHAR", "DATE", "TIMESTAMP", "BOOLEAN",
    )
    if hasattr(exp.DataType.Type, name)
)  # fmt: skip


# The only types an expression that reads a personal column may cast to (V22). A cast to another
# type fails on a value that does not fit, and PostgreSQL then prints the value in its error
# message, which a caller that runs the SQL sees.
TEXT_CAST_TYPES: frozenset[object] = frozenset(
    getattr(exp.DataType.Type, name)
    for name in ("TEXT", "VARCHAR")
    if hasattr(exp.DataType.Type, name)
)


def first_value_dependent_cast(tree: exp.Expr) -> str | None:
    """The first cast in `tree` to a type that is not text, or a function that casts to a date
    inside (`TS_OR_DS_TO_DATE`), as a phrase; None if there is none."""
    for node in tree.walk():
        if isinstance(node, exp.Cast):
            target = node.args.get("to")
            if not isinstance(target, exp.DataType) or target.this not in TEXT_CAST_TYPES:
                shown = target.sql() if isinstance(target, exp.DataType) else "an unknown type"
                return "a cast to " + shown
        elif isinstance(node, exp.TsOrDsToDate):
            return "a conversion to a date"
    return None


# The parts of `EXTRACT(part FROM x)` that PostgreSQL and DuckDB return alike (checked on a
# timestamp with fractional seconds by tests/engine/test_function_parity.py). `SECOND`, `EPOCH`,
# and `MILLISECOND` come back with fractions on PostgreSQL and without on DuckDB, and
# `TIMEZONE` does not exist for a timestamp on PostgreSQL.
ALLOWED_EXTRACT_PARTS: frozenset[str] = frozenset(
    {
        "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "QUARTER", "WEEK", "DOW", "DOY",
        "MICROSECOND", "DECADE", "CENTURY", "MILLENNIUM", "ISODOW", "ISOYEAR",
    }
)  # fmt: skip


def _integer_literal(node: object) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string and node.name.isdigit():
        return int(node.name)
    return None


def function_name(node: exp.Expr) -> str:
    """The name of a function node as written in SQL."""
    if isinstance(node, exp.Func) and not isinstance(node, exp.Anonymous):
        return node.sql_name().lower()
    return node.name.lower()


def is_allowed_function(node: exp.Expr) -> bool:
    """True if `node` is not a function, or is one on the allow-list. Aggregates and windows are
    not scalar functions and are judged elsewhere."""
    return node_problem(node) is None


def node_problem(node: exp.Expr) -> str | None:
    """Why `node` may not appear in a definition expression or a finished query, or None.

    - a function that is not on the allow-list (named);
    - a schema-qualified function name such as `public.btrim(x)`, which calls whatever function
      of that schema has the name;
    - a cast to a type that is not on the list of cast types;
    - date arithmetic that PostgreSQL and DuckDB read differently: `DATE_ADD`, `DATE_SUB`, and
      `TS_OR_DS_ADD` with no unit (PostgreSQL adds a second, DuckDB a day), and `DATEDIFF`
      (PostgreSQL divides a number of seconds, DuckDB counts day boundaries). Write
      `d + INTERVAL '1 day'` and `d1 - d2`;
    - `ROUND` (PostgreSQL rounds a double half to even, DuckDB half away from zero), `SUBSTRING`
      unless its start is a positive and its length a non-negative literal whole number (a start
      below 1 counts from the other end on DuckDB), `EXTRACT` of a part the engines return
      differently (`SECOND`, `EPOCH`, `MILLISECOND`), `TRY_CAST`, a JSON cast, and a cast to a
      text type with a length (PostgreSQL truncates);
    - `GREATEST` and `LEAST`: PostgreSQL skips NULL arguments and DuckDB returns NULL, so they
      give different numbers (write a `CASE` expression);
    - subscripting a value (`segment['email']`, `x[1]`): on a JSON or hstore column it takes a key,
      as `->` does, which is refused too;
    - field selection on a value (`(segment).email`): the name after the dot is not a column of
      the table, so no check on columns can compare it with the personal columns;
    - raw text (`exp.Command`): sqlglot keeps a text it cannot read, such as one starting with
      `IF`, as a string that the printer writes out unchanged, so nothing in it was checked.
    """
    if isinstance(node, exp.Command):
        return "raw text that is not an expression"
    if isinstance(node, (exp.Greatest, exp.Least)):
        return (
            f"the function {function_name(node)}, which PostgreSQL ignores NULL in and DuckDB "
            "does not (write a CASE expression)"
        )
    if isinstance(node, exp.TryCast):
        return "TRY_CAST, which gives NULL on DuckDB and an error on PostgreSQL"
    if isinstance(node, exp.JSONCast):
        return "a cast to JSON, which DuckDB and PostgreSQL read differently"
    if isinstance(node, exp.Round):
        return (
            "the function round, which rounds a double half to even on PostgreSQL and half away "
            "from zero on DuckDB"
        )
    if isinstance(node, exp.Substring):
        start, length = _integer_literal(node.args.get("start")), node.args.get("length")
        if not start or (length is not None and _integer_literal(length) is None):
            return (
                "SUBSTRING with a start or length that is not a literal whole number (a start "
                "below 1 counts from the other end on DuckDB and not on PostgreSQL; write a "
                "positive literal start and a literal length)"
            )
    if isinstance(node, exp.Extract):
        part = node.this.name.upper() if node.this is not None else ""
        if part not in ALLOWED_EXTRACT_PARTS:
            return (
                f"EXTRACT of the part {part or 'unknown'}, which PostgreSQL and DuckDB return "
                "differently"
            )
    if isinstance(node, exp.Bracket):
        return "subscripting a value"
    if isinstance(node, (exp.DateDiff, exp.TsOrDsDiff)):
        return "DATEDIFF, which PostgreSQL and DuckDB count differently (write d1 - d2)"
    if isinstance(node, (exp.DateAdd, exp.DateSub, exp.TsOrDsAdd)):
        if node.args.get("unit") is None and not isinstance(node.expression, exp.Interval):
            return (
                "date arithmetic with no unit, which PostgreSQL reads as seconds and DuckDB as "
                "days (write d + INTERVAL '1 day')"
            )
    if isinstance(node, exp.Dot):
        if isinstance(node.expression, exp.Func):
            return "the schema-qualified function " + function_name(node.expression)
        return "field selection on a value"
    if isinstance(node, exp.Cast):
        target = node.args.get("to")
        if not isinstance(target, exp.DataType) or target.this not in ALLOWED_CAST_TYPES:
            shown = target.sql() if isinstance(target, exp.DataType) else "an unknown type"
            return "a cast to the type " + shown
        if target.expressions and target.this in (
            exp.DataType.Type.VARCHAR,
            exp.DataType.Type.TEXT,
        ):
            return (
                "a cast to a text type with a length, which PostgreSQL truncates to and DuckDB "
                "does not"
            )
    if isinstance(node, exp.Anonymous):
        return (
            None
            if function_name(node) in ALLOWED_ANONYMOUS
            else "the function " + function_name(node)
        )
    if isinstance(node, exp.Func) and not isinstance(node, ALLOWED_NODES):
        return "the function " + function_name(node)
    return None


def first_disallowed_function(tree: exp.Expr) -> str | None:
    """What the first disallowed function, qualified function name, or cast type in `tree` is,
    as a phrase such as `the function set_config`. Aggregates and windows are skipped."""
    for node in tree.walk():
        if isinstance(node, (exp.AggFunc, exp.Window)):
            continue
        problem = node_problem(node)
        if problem is not None:
            return problem
    return None
