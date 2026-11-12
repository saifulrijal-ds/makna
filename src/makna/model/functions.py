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
    "Cast", "Case", "If", "Coalesce", "Nullif", "Greatest", "Least", "And", "Or", "Xor", "Not",
    # arithmetic and rounding
    "Abs", "Round", "Floor", "Ceil", "Sign", "Sqrt", "Pow", "Mod",
    # text
    "Lower", "Upper", "Initcap", "Trim", "Substring", "Length", "Concat", "ConcatWs",
    "Left", "Right", "Replace", "StrPosition", "SplitPart", "Pad",
    # dates: truncation, extraction, differences
    "TimestampTrunc", "DateTrunc", "Extract", "Year", "Month", "Day", "Quarter", "Week",
    "DayOfWeek", "DayOfMonth", "DayOfYear", "DateDiff", "DateAdd", "DateSub", "TsOrDsToDate",
    "TsOrDsAdd", "TsOrDsDiff", "ToChar",
)  # fmt: skip
ALLOWED_NODES: tuple[type[exp.Expr], ...] = tuple(
    node for name in _NODE_NAMES if isinstance(node := getattr(exp, name, None), type)
)
ALLOWED_ANONYMOUS: frozenset[str] = frozenset({"btrim", "ltrim", "rtrim", "date_part"})


# Types a cast may target. Everything else is refused, `reg*` types and user-defined types
# included: casting a personal value to a type that can reject it (`CAST(full_name AS INTEGER)`,
# `CAST(national_id AS regclass)`) makes the engine print the value in its error message.
ALLOWED_CAST_TYPES: frozenset[object] = frozenset(
    getattr(exp.DataType.Type, name)
    for name in (
        "TINYINT", "SMALLINT", "INT", "BIGINT", "DECIMAL", "FLOAT", "DOUBLE",
        "TEXT", "VARCHAR", "CHAR", "DATE", "TIMESTAMP", "TIMESTAMPTZ", "BOOLEAN",
    )
    if hasattr(exp.DataType.Type, name)
)  # fmt: skip


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
    - a cast to a type that is not on the list of cast types.
    """
    if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
        return "the schema-qualified function " + function_name(node.expression)
    if isinstance(node, exp.Cast):
        target = node.args.get("to")
        if not isinstance(target, exp.DataType) or target.this not in ALLOWED_CAST_TYPES:
            shown = target.sql() if isinstance(target, exp.DataType) else "an unknown type"
            return "a cast to the type " + shown
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
