"""Governance of personal data (ARCHITECTURE section 7.6, QRY-2): masks, and the final check.

`mask_expression` builds the SQL node that replaces a personal dimension's value. `verify` walks
the finished syntax tree and fails if a column that is personal (read by a personal dimension, or
tagged personal by the catalog) appears anywhere except:

- inside a mask that the plan put in the query: a whole select-list item under the alias of a
  selected masked dimension (or the matching GROUP BY item), with the shape and parameters of
  that dimension's own masking rule; or
- as the whole, bare argument of `COUNT(col)` or `COUNT(DISTINCT col)`,

and then only in the select list or the GROUP BY of its own query. A mask-shaped expression from a
definition gets no exemption. The check catches mistakes in the generator itself; it is not a
second planner and does not look at the model.

The governance policy of the plan (D-29) widens what is accepted, and nothing else. Under `strict`
the rules above apply as they are. Under `custom` (and `off`, which is `custom` with every rule
relaxed: the checks are the same, only the policy's values differ) a raw personal column is also
accepted where an enabled rule allows it and where the plan put it:

- in a whole select-list item, or the equal GROUP BY item, under the alias of a personal
  dimension the plan selected raw (or `metric_time`, for a grain the policy released), when the
  policy lets that column be returned raw (`allow_unmasked_personal` for a dimension with no
  masking rule, `apply_masks = false` for one with a rule);
- inside the WHERE of an input query (its filters, time range, and month-end selection) when
  `allow_personal_filters`.

Every other case fails, in particular `MIN`, `MAX`, `SUM`, and `AVG` of a personal column, and a
raw column under any other alias, in a subquery or lateral item, or in HAVING, ORDER BY, or a join.
A function that is not on the allow-list of `makna.model.functions` (or built by the compiler)
fails in every mode, because a function such as `set_config` can carry a value past a check on
columns.

All SQL is built as sqlglot nodes. No text is formatted or concatenated.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from makna.model.definitions import MaskBucket, Masking, MaskRedact, MaskTruncateDate
from makna.model.functions import node_problem
from makna.model.governance import GovernancePolicy
from makna.model.sensitivity import columns_read

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


def raw_returnable_columns(model: Model, policy: GovernancePolicy) -> frozenset[tuple[str, str]]:
    """`(table, column)`, lower case, of the personal columns the select list and GROUP BY may
    return unmasked under `policy`: those every personal dimension reading them may return raw
    (`GovernancePolicy.may_return_raw`). A column the catalog tags personal that no dimension
    reads is not in the set, so nothing may return it. Empty under `strict`."""
    allowed: set[tuple[str, str]] = set()
    refused: set[tuple[str, str]] = set()
    for entity in model.entities.values():
        table = entity.source.table.lower()
        for dimension in entity.dimensions:
            if model.effective_sensitivity(entity, dimension) != "personal":
                continue
            ok = policy.may_return_raw(dimension.masking is not None)
            target = allowed if ok else refused
            target.update((table, name.lower()) for name in columns_read(dimension))
    return frozenset(allowed - refused)


# --------------------------------------------------------------------------------------
# The final check


class GovernanceError(RuntimeError):
    """The generated SQL breaks a personal-data rule. This is a bug in the generator or in the
    model, never a mistake in a request, so it is not a `QueryError`.

    When the rule broken is the one about a personal column, `table` and `column` name it (lower
    case, as the model keys them). They are names, never values. The service layer turns such an
    error into a structured refusal (R05).
    """

    def __init__(
        self, message: str, *, table: str | None = None, column: str | None = None
    ) -> None:
        super().__init__(message)
        self.table = table
        self.column = column


def _violation(column: str, table: str, where: str) -> GovernanceError:
    return GovernanceError(
        f"internal error: the personal column {table}.{column} appears {where}. A personal "
        "column may appear only inside a mask or a count, in the select list or the group clause.",
        table=table,
        column=column,
    )


def _function_violation(problem: str) -> GovernanceError:
    return GovernanceError(
        f"internal error: the query uses {problem}, which is not on the list of functions and "
        "cast types a query may use (makna.model.functions), so a value could pass through it "
        "unchecked"
    )


_Masked = dict[str, Masking | None]
# Clauses of a SELECT in which a masked or counted personal column may sit.
_ALLOWED_CLAUSES = ("expressions", "group")
_MASK_GRAINS = ("month", "quarter", "year")


def verify(tree: exp.Expr, plan: QueryPlan) -> None:
    """Fail unless every reference to a personal column in `tree` is allowed.

    Personal columns are those in `plan.personal_columns`: all columns read by any dimension of
    the model whose effective sensitivity is personal or that the catalog tags personal. A
    reference is allowed only inside a mask that the plan put in the query (see
    `_is_planned_mask`; a mask-shaped expression from a definition is not exempt) or as the
    whole, bare argument of `COUNT(col)` or `COUNT(DISTINCT col)` (not
    `COUNT(CASE WHEN col < x ...)`, which would reveal a test on one person's value),
    and only in the select list or the GROUP BY of its query: never in WHERE, HAVING, ORDER BY,
    or a join condition. `MIN`, `MAX`, and other aggregates do not count, because they return one
    person's value. `SELECT *` over a table that holds a personal column fails too.

    The plan's governance policy can widen this (see the module docstring): a raw personal
    column is accepted where an enabled rule allows it and the plan put it. `off` is `custom`
    with every rule relaxed; the checks do not skip. A function that is neither an aggregate,
    built by the compiler, nor on the allow-list fails in every mode.

    Raises `GovernanceError`. It does not plan again and does not read the model.
    """
    for node in tree.walk():
        if isinstance(node, (exp.AggFunc, exp.Window)):
            continue
        problem = node_problem(node)
        if problem is not None:
            raise _function_violation(problem)
    personal = plan.personal_columns
    if not personal:
        return
    masked = {d.column: d.dimension.masking for d in plan.dimensions if d.masked}
    for node in tree.find_all(exp.Column, exp.Star):
        if isinstance(node, exp.Star) and isinstance(node.parent, (exp.Column, exp.Count)):
            continue  # `t.*` is checked as a column; `COUNT(*)` reads no value
        for table, column in _personal_reads(node, personal):
            if not _allowed(node, masked) and not _raw_allowed(node, (table, column), plan):
                raise _violation(column, table, _where(node))


def _where(node: exp.Expr) -> str:
    if isinstance(node.parent, exp.AggFunc):
        return "inside an aggregate that is not a count"
    return (
        "outside a mask the plan built (with that dimension's masking rule) or a bare COUNT, "
        "or in a clause that may not hold it"
    )


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


def _allowed(node: exp.Expr, masked: _Masked) -> bool:
    """True if `node` sits inside a mask the plan built, or is the bare argument of a COUNT, in
    an allowed clause of its own query."""
    covered = False
    child: exp.Expr = node
    for parent in _ancestors(node):
        if isinstance(parent, exp.Select):
            return covered and child.arg_key in _ALLOWED_CLAUSES
        if not covered and is_mask(parent) and _is_planned_mask(parent, masked):
            covered = True
        elif not covered and isinstance(parent, exp.Count):
            if not _is_counted_bare(parent, node):
                return False  # an expression around the column reveals a value about it
            covered = True
        child = parent
    return covered


def _raw_allowed(node: exp.Expr, pair: tuple[str, str], plan: QueryPlan) -> bool:
    """True if the policy of the plan lets this raw personal column stand where it is, and the
    plan put it there (as `_is_planned_mask` does for masks).

    - Inside the WHERE of a query at the level of an input query (not inside a select-list
      expression or a join): when `allow_personal_filters`. This covers the filters, the time
      range, and the month-end subquery the plan built.
    - As a whole select-list item, or the equal GROUP BY item, of such a query, under the alias
      of a dimension the plan selected raw (or `metric_time`), in an expression that holds no
      aggregate, window, or subquery: when the column is one of the plan's `raw_columns`.
    - Nowhere else: not in HAVING, ORDER BY, a join condition, or another select-list item.
    """
    if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
        return False  # a star returns whatever the table holds; it is never accepted
    wheres = [a for a in _ancestors(node) if isinstance(a, exp.Where)]
    if wheres:
        outer = wheres[-1]
        return plan.governance.allow_personal_filters and (
            isinstance(outer.parent, exp.Select) and _query_level(outer.parent)
        )
    if pair not in plan.raw_columns:
        return False
    expected = _planned_raw_items(plan)
    child: exp.Expr = node
    item: exp.Expr = node  # the whole select-list or GROUP BY item that holds the column
    for parent in _ancestors(node):
        if isinstance(parent, (exp.AggFunc, exp.Window, exp.Subquery)):
            return False
        if not isinstance(parent, (exp.Select, exp.Group)):
            item = parent
        if isinstance(parent, exp.Select):
            if not _query_level(parent):
                return False
            if child.arg_key == "expressions":
                return isinstance(child, exp.Alias) and _is_planned_item(child, expected)
            if child.arg_key == "group":
                return any(
                    isinstance(other, exp.Alias)
                    and other.this == item
                    and _is_planned_item(other, expected)
                    for other in parent.expressions
                )
            return False
        child = parent
    return False


def _normalized(expression: exp.Expr) -> exp.Expr:
    """`expression` without table qualifiers and quoting, so that the text a definition holds
    can be compared with the column bound to a table alias in the query."""
    copy = expression.copy()
    for column in copy.find_all(exp.Column):
        column.set("table", None)
        column.this.set("quoted", False)
    return copy


def _planned_raw_items(plan: QueryPlan) -> dict[str, list[exp.Expr]]:
    """The expressions the plan built for each alias that may hold a raw personal column: for a
    dimension selected raw, the dimension's own expression; for `metric_time`, the grain applied
    to the official date of an input. The query's item must equal one of them."""
    found: dict[str, list[exp.Expr]] = {}
    for planned in plan.dimensions:
        if planned.personal and not planned.masked:
            text = planned.dimension.expr or planned.dimension.name
            found[planned.column] = [_normalized(_parse_condition(text))]
    if plan.grain is not None:
        found["metric_time"] = [
            _normalized(
                exp.Cast(
                    this=exp.TimestampTrunc(
                        this=_parse_condition(item.time_dimension.expr or item.time_dimension.name),
                        unit=exp.Literal.string(plan.grain),
                    ),
                    to=exp.DataType.build("date"),
                )
            )
            for item in plan.inputs
        ]
    return found


def _parse_condition(text: str) -> exp.Expr:
    try:
        return sqlglot.parse_one(text, into=exp.Condition)
    except SqlglotError:
        return exp.column(text)


def _is_planned_item(item: exp.Alias, expected: dict[str, list[exp.Expr]]) -> bool:
    return item.alias in expected and _normalized(item.this) in expected[item.alias]


def _query_level(select: exp.Select) -> bool:
    """True if `select` is a query of its own: the root, or the body of a CTE. A select inside
    an expression, a join, a lateral, or a subquery is not."""
    child: exp.Expr = select
    for parent in _ancestors(select):
        if isinstance(parent, exp.Select) and child.arg_key != "with_":
            return False
        if isinstance(parent, (exp.Subquery, exp.Where, exp.Lateral, exp.Join)):
            return False
        child = parent
    return True


def _is_planned_mask(mask: exp.Expr, masked: _Masked) -> bool:
    """True if `mask` is where the plan put one: a whole select-list item under the alias of a
    planned masked dimension, or a whole GROUP BY item equal to such an item of the same query.
    A mask-shaped expression anywhere else (inside `MAX`, `SUM`, or `COUNT`, or in a measure or
    a dimension that is not masked) is not a mask and gets no exemption."""
    parent = mask.parent
    if isinstance(parent, exp.Alias) and parent.this is mask:
        return _has_rule(mask, parent.alias, masked) and isinstance(parent.parent, exp.Select)
    if isinstance(parent, exp.Group) and isinstance(parent.parent, exp.Select):
        return any(
            isinstance(item, exp.Alias)
            and item.this == mask
            and _has_rule(mask, item.alias, masked)
            for item in parent.parent.expressions
        )
    return False


def _has_rule(mask: exp.Expr, alias: str, masked: _Masked) -> bool:
    """True if `mask` is the masking rule of the dimension planned under `alias`: the same
    method and the same grain or width."""
    rule = masked.get(alias)
    return rule is not None and _signature(mask) == _rule_signature(rule)


def _signature(mask: exp.Expr) -> tuple[str, object] | None:
    if isinstance(mask, exp.Cast) and isinstance(mask.this, exp.TimestampTrunc):
        return ("truncate_date", _unit(mask.this))
    if isinstance(mask, exp.Mul):
        return ("bucket", float(mask.expression.name))
    return None


def _rule_signature(rule: Masking) -> tuple[str, object] | None:
    if isinstance(rule, MaskTruncateDate):
        return ("truncate_date", rule.grain)
    if isinstance(rule, MaskBucket):
        return ("bucket", float(rule.width))
    return None


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
