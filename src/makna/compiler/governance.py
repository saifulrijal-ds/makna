"""Governance of personal data (ARCHITECTURE section 7.6, QRY-2): masks, and the final check.

`mask_expression` builds the SQL node that replaces a personal dimension's value. `verify` walks
the finished syntax tree and fails if any column read by a personal dimension appears anywhere
except inside a mask or inside `COUNT(...)` or `COUNT(DISTINCT ...)`. The check catches mistakes
in the generator itself; it is not a second planner and does not look at the model.

All SQL is built as sqlglot nodes. No text is formatted or concatenated.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from makna.model.definitions import Dimension, MaskBucket, MaskRedact, MaskTruncateDate

if TYPE_CHECKING:
    from makna.compiler.plan import QueryPlan
    from makna.compiler.snapshot import Model

# The result column that says how many groups the minimum group size left out. Its name starts
# with two underscores, which no entity, dimension, or metric name can (names start with a
# letter), so it cannot clash with a `<entity>__<dimension>` or a metric column.
GROUPS_LEFT_OUT = "__groups_left_out"
# Prefix of the per-entity record counts inside the generated SQL (never returned).
RECORDS_PREFIX = "__records_"

REDACTED = "***"


def mask_expression(
    masking: MaskRedact | MaskTruncateDate | MaskBucket, value: exp.Expr
) -> exp.Expr:
    """The expression that replaces `value` for a personal dimension.

    - `redact`: the constant `***`, typed (PostgreSQL rejects a bare string constant in GROUP BY).
    - `truncate_date`: the first day of the month, quarter, or year, as a date.
    - `bucket`: the lower bound of the band of the given width. The value is cast to a floating
      type first, because `a / b` on two integers truncates on PostgreSQL and not on DuckDB, and
      truncation toward zero would give the wrong band for negative values.
    """
    if isinstance(masking, MaskRedact):
        return exp.Cast(this=exp.Literal.string(REDACTED), to=exp.DataType.build("text"))
    if isinstance(masking, MaskTruncateDate):
        return exp.Cast(
            this=exp.TimestampTrunc(this=value, unit=exp.Literal.string(masking.grain)),
            to=exp.DataType.build("date"),
        )
    width = _number(masking.width)
    banded = exp.Floor(
        this=exp.Div(
            this=exp.Cast(this=value, to=exp.DataType.build("double")), expression=width.copy()
        )
    )
    return exp.Mul(this=banded, expression=width)


def _number(value: float) -> exp.Expr:
    return exp.Literal.number(int(value) if float(value).is_integer() else value)


# --------------------------------------------------------------------------------------
# Which columns are personal


def columns_read(dimension: Dimension) -> tuple[str, ...]:
    """Names of the columns a dimension's expression reads, as written.

    A dimension with no `expr` reads the column of its own name. An expression that does not
    parse reads the dimension's own name: validation (V05) rejects such files, and a broken
    expression elsewhere must not stop a request that never uses it.
    """
    if dimension.expr is None:
        return (dimension.name,)
    try:
        tree = sqlglot.parse_one(dimension.expr, into=exp.Condition)
    except SqlglotError:
        return (dimension.name,)
    return tuple(dict.fromkeys(c.name for c in tree.find_all(exp.Column)))


def personal_columns(model: Model) -> frozenset[tuple[str, str]]:
    """`(table, column)`, lower case, for every column that is personal in either way: read by
    a dimension whose effective sensitivity is personal (selected or not), or tagged personal by
    the catalog itself (`model.derived_sensitivity`), whether or not any dimension reads it.
    `table` is the entity's `source.table`."""
    found: set[tuple[str, str]] = set()
    for entity in model.entities.values():
        table = entity.source.table.lower()
        for dimension in entity.dimensions:
            if model.effective_sensitivity(entity, dimension) == "personal":
                found.update((table, name.lower()) for name in columns_read(dimension))
    for (table, column), level in model.derived_sensitivity.items():
        if level == "personal":
            found.add((table.lower(), column.lower()))
    return frozenset(found)


# --------------------------------------------------------------------------------------
# The final check


class GovernanceError(RuntimeError):
    """The generated SQL breaks a personal-data rule. This is a bug in the generator or in the
    model, never a mistake in a request, so it is not a `QueryError`."""


def _violation(column: str, table: str, where: str) -> GovernanceError:
    return GovernanceError(
        f"internal error: the personal column {table}.{column} appears {where}. A personal "
        "column may appear only inside a mask or a count, in the select list or the group clause."
    )


# Clauses of a SELECT in which a masked or counted personal column may sit.
_ALLOWED_CLAUSES = ("expressions", "group")
_MASK_GRAINS = ("month", "quarter", "year")


def verify(tree: exp.Expr, plan: QueryPlan) -> None:
    """Fail unless every reference to a personal column in `tree` is allowed.

    Personal columns are those in `plan.personal_columns`: all columns read by any dimension of
    the model whose effective sensitivity is personal. A reference is allowed only inside a mask
    (`mask_expression`) or as the whole, bare argument of `COUNT(col)` or `COUNT(DISTINCT col)`
    (not `COUNT(CASE WHEN col < x ...)`, which would reveal a test on one person's value),
    and only in the select list or the GROUP BY of its query: never in WHERE, HAVING, ORDER BY,
    or a join condition. `MIN`, `MAX`, and other aggregates do not count, because they return one
    person's value. `SELECT *` over a table that holds a personal column fails too.

    Raises `GovernanceError`. It does not plan again and does not read the model.
    """
    personal = plan.personal_columns
    if not personal:
        return
    for node in tree.find_all(exp.Column, exp.Star):
        if isinstance(node, exp.Star) and isinstance(node.parent, (exp.Column, exp.Count)):
            continue  # `t.*` is checked as a column; `COUNT(*)` reads no value
        for table, column in _personal_reads(node, personal):
            if not _allowed(node):
                raise _violation(column, table, _where(node))


def _where(node: exp.Expr) -> str:
    if isinstance(node.parent, exp.AggFunc):
        return "inside an aggregate that is not a count"
    return "outside a mask or COUNT, or in a clause that may not hold it"


def _parts(table: exp.Table) -> tuple[str, ...]:
    return tuple(p.lower() for p in (table.catalog, table.db, table.name) if p)


def _is_table(given: tuple[str, ...], personal: str) -> bool:
    """A table named `lending.core.customers` or just `customers` is the personal table; a
    shorter name is matched by its end, which is the safe side."""
    return bool(given) and tuple(personal.split(".")[-len(given) :]) == given


def _sources(select: exp.Select) -> list[exp.Table]:
    sources = [select.args["from_"].this] if select.args.get("from_") else []
    sources.extend(join.this for join in select.args.get("joins") or [])
    return [s for s in sources if isinstance(s, exp.Table)]


def _handle(table: exp.Table) -> str:
    return (table.alias or table.name).lower()


def _personal_reads(node: exp.Expr, personal: frozenset[tuple[str, str]]) -> list[tuple[str, str]]:
    """The `(table, column)` pairs `node` reads that are personal. Without a usable scope the
    match is by column name alone."""
    if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
        names: Iterable[str] | None = (node.name.lower(),)
    else:
        names = None  # a star: every column of the table
    qualifier = node.table.lower() if isinstance(node, exp.Column) and node.table else None
    scopes = [s for s in _ancestors(node) if isinstance(s, exp.Select)]
    if not scopes:
        return sorted((t, c) for t, c in personal if names is None or c in names)
    found: set[tuple[str, str]] = set()
    for scope in scopes:
        tables = _sources(scope)
        if qualifier is not None:
            tables = [t for t in tables if _handle(t) == qualifier]
            if not tables:
                continue
        for table in tables:
            for t, c in personal:
                if _is_table(_parts(table), t) and (names is None or c in names):
                    found.add((t, c))
        if qualifier is not None:
            break  # a qualified name is resolved by the nearest scope that has the alias
    return sorted(found)


def _ancestors(node: exp.Expr) -> list[exp.Expr]:
    found: list[exp.Expr] = []
    parent = node.parent
    while parent is not None:
        found.append(parent)
        parent = parent.parent
    return found


def _allowed(node: exp.Expr) -> bool:
    """True if `node` sits inside a mask or a COUNT of its own query, in an allowed clause."""
    covered = False
    child: exp.Expr = node
    for parent in _ancestors(node):
        if isinstance(parent, exp.Select):
            return covered and child.arg_key in _ALLOWED_CLAUSES
        if not covered and is_mask(parent):
            covered = True
        elif not covered and isinstance(parent, exp.Count):
            if not _is_counted_bare(parent, node):
                return False  # an expression around the column reveals a value about it
            covered = True
        child = parent
    return covered


def _is_counted_bare(count: exp.Count, node: exp.Expr) -> bool:
    """True if `node` is the whole argument of `COUNT(node)` or `COUNT(DISTINCT node)`."""
    argument = count.this
    if isinstance(argument, exp.Distinct):
        return len(argument.expressions) == 1 and argument.expressions[0] is node
    return argument is node


def is_mask(node: exp.Expr) -> bool:
    """True if `node` has the shape `mask_expression` gives to `truncate_date` or `bucket`.

    `redact` reads no column, so it needs no recognising. The shape is checked closely on
    purpose: `CAST(date AS DATE)` or a truncation to the day is not a mask.
    """
    if isinstance(node, exp.Cast):
        inner = node.this
        return (
            node.to.is_type("date")
            and isinstance(inner, exp.TimestampTrunc)
            and _unit(inner) in _MASK_GRAINS
        )
    if isinstance(node, exp.Mul) and isinstance(node.this, exp.Floor):
        div = node.this.this
        width = node.expression
        return (
            isinstance(div, exp.Div)
            and isinstance(div.this, exp.Cast)
            and div.this.to.is_type("double")
            and _positive_number(width)
            and _positive_number(div.expression)
            and float(width.name) == float(div.expression.name)
        )
    return False


def _unit(trunc: exp.TimestampTrunc) -> str | None:
    """The unit of a truncation, whether sqlglot holds it as a string or as a bare word."""
    unit = trunc.args.get("unit")
    if isinstance(unit, exp.Var) or (isinstance(unit, exp.Literal) and unit.is_string):
        return unit.name.lower()
    return None


def _positive_number(node: exp.Expr) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and float(node.name) > 0
