"""SQL generation (ARCHITECTURE section 7.5): a query plan becomes a sqlglot syntax tree, and the
tree is printed for a dialect.

`build_tree` is a pure function of the model snapshot and the plan and decides nothing new: the
joins, the filters, and the columns were all settled by planning. The tree is dialect-neutral;
`to_sql` prints it for `postgres` (the engine) or `duckdb` (unit tests).

Every simple input is its own subquery from its own table, joined only from the "many" side to
the "one" side. Inputs of a ratio or derived metric are combined with `UNION ALL` and `GROUP BY`
on the shared keys, never joined row to row (docs/notes/never-join-fact-tables.md).

No SQL text is built by string formatting or concatenation anywhere in this package. Expressions
from the definitions are parsed and their columns bound to a table alias; values from a request
become literal nodes. A test scans the package source to keep it that way.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from makna.compiler.graph import JoinStep
from makna.compiler.plan import (
    InputPlan,
    MetricNode,
    PlannedFilter,
    PlannedRule,
    QueryPlan,
)
from makna.compiler.snapshot import Model
from makna.model.definitions import Dimension, Entity, Scalar
from makna.model.request import ErrorCode, QueryError

Dialect = Literal["postgres", "duckdb"]

TIME_COLUMN = "metric_time"
_COMPARE: dict[str, type[exp.Binary]] = {
    "=": exp.EQ,
    "!=": exp.NEQ,
    ">": exp.GT,
    ">=": exp.GTE,
    "<": exp.LT,
    "<=": exp.LTE,
}
_PLAIN_AGGS: dict[str, type[exp.AggFunc]] = {
    "sum": exp.Sum,
    "avg": exp.Avg,
    "min": exp.Min,
    "max": exp.Max,
}
# Aggregates whose value is 0, not NULL, when an input has no rows for a group (section 7.5).
_ZERO_WHEN_MISSING = ("sum", "count", "count_distinct")


def to_sql(tree: exp.Expr, dialect: Dialect, *, pretty: bool = True) -> str:
    """Print `tree` for `dialect`. A construct the dialect cannot write raises instead of being
    printed approximately."""
    return tree.sql(dialect=dialect, pretty=pretty, unsupported_level=ErrorLevel.RAISE)


def reparse(sql: str, dialect: Dialect) -> exp.Expr:
    """Read SQL text back with sqlglot's parser for `dialect`; `SqlglotError` if it cannot."""
    return sqlglot.parse_one(sql, read=dialect)


def build_tree(model: Model, plan: QueryPlan) -> exp.Select:
    """The whole query for `plan`, with `ORDER BY` and `LIMIT`."""
    nodes = {node.name: node for node in plan.nodes}
    root = nodes[plan.metric]
    keys = _key_names(plan)
    leaves = {item.metric: _input_query(model, plan, item) for item in plan.inputs}
    if root.type == "simple":
        query = leaves[root.name]
    else:
        query = _combine(plan, nodes, root, keys, leaves)
    if keys:
        query = query.order_by(*[exp.Ordered(this=_name(k), nulls_first=False) for k in keys])
    return query.limit(plan.limit)


# --------------------------------------------------------------------------------------
# Names and small builders


def _key_names(plan: QueryPlan) -> list[str]:
    time = [TIME_COLUMN] if plan.grain is not None else []
    return time + [d.column for d in plan.dimensions]


def _name(column: str) -> exp.Column:
    return exp.column(column, quoted=True)


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _table(entity: Entity, alias: str) -> exp.Table:
    catalog, schema, name = entity.source.table.split(".")
    return exp.table_(name, db=schema, catalog=catalog, alias=_ident(alias), quoted=True)


def _failed(what: str, sql: str, detail: str) -> QueryError:
    return QueryError(
        ErrorCode.BROKEN_REFERENCE,
        f"The {what} {sql!r} cannot be used in a query ({detail}). A curator must fix the "
        "definition before the metric can be used.",
        {"issues": [{"code": "INVALID_EXPRESSION", "object": what, "message": detail}]},
    )


def _refused(code: ErrorCode, subject: object, text: str) -> QueryError:
    return QueryError(code, f"{subject!r} {text}")


def _alias(index: int) -> str:
    """A table alias such as `t3`; it only ever goes into a quoted identifier node."""
    return f"t{index}"


def _cte(metric: str) -> str:
    """The name of an input's CTE, `m_<metric>`; it only ever goes into an identifier node."""
    return f"m_{metric}"


def _cte_identifier(metric: str) -> exp.Identifier:
    return _ident(_cte(metric))


@lru_cache(maxsize=1024)
def _parsed(sql: str) -> exp.Expr:
    return sqlglot.parse_one(sql, into=exp.Condition)


def _bind(sql: str, alias: str) -> exp.Expr:
    """A scalar expression from a definition, with every column pointing at table `alias`."""
    try:
        tree = _parsed(sql).copy()
    except SqlglotError as error:
        raise _failed("expression", sql, str(error).splitlines()[0]) from None
    if tree.find(exp.Query, exp.AggFunc, exp.Window) is not None:
        raise _failed("expression", sql, "subqueries, aggregates, and windows are not allowed")
    for column in tree.find_all(exp.Column):
        column.set("table", _ident(alias))
        column.this.set("quoted", True)
    return tree


def _literal(value: Scalar, dimension: Dimension) -> exp.Expr:
    """A request value as a literal node. Text is always a string literal, never SQL."""
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise _refused(ErrorCode.INVALID_FILTER, value, "is not a usable number.")
        return exp.Literal.number(value)
    if dimension.type == "time":
        kind = "date" if _is_date(value) else "timestamp"
        return exp.Cast(this=exp.Literal.string(value), to=exp.DataType.build(kind))
    return exp.Literal.string(value)


def _is_date(text: str) -> bool:
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return len(text) == 10


def _date(value: datetime.date) -> exp.Expr:
    return exp.Cast(this=exp.Literal.string(value.isoformat()), to=exp.DataType.build("date"))


def _trunc(grain: str, value: exp.Expr) -> exp.Expr:
    return exp.Cast(
        this=exp.TimestampTrunc(this=value, unit=exp.Literal.string(grain)),
        to=exp.DataType.build("date"),
    )


def _condition(
    column: exp.Expr, op: str, value: Scalar | tuple[Scalar, ...] | None, dimension: Dimension
) -> exp.Expr:
    """One structured filter as a condition on `column`."""
    if op in _COMPARE and not isinstance(value, tuple) and value is not None:
        return _COMPARE[op](this=column, expression=_literal(value, dimension))
    if op in ("in", "not_in") and isinstance(value, tuple):
        member = exp.In(this=column, expressions=[_literal(v, dimension) for v in value])
        return exp.Not(this=member) if op == "not_in" else member
    if op in ("is_null", "is_not_null") and value is None:
        empty = exp.Is(this=column, expression=exp.Null())
        return exp.Not(this=empty) if op == "is_not_null" else empty
    raise _refused(ErrorCode.INVALID_FILTER, op, "is not a filter operator that fits its value.")


# --------------------------------------------------------------------------------------
# One simple input


class _Joins:
    """Table aliases for one input: `t0` for the measure's table, one more per join.

    Aliases belong to join prefixes, not to relationship names, so two paths that pass through
    the same entity by different routes get separate joins and never share the wrong one.
    """

    def __init__(self, model: Model, item: InputPlan) -> None:
        self.measure_entity = model.entities[item.measure_entity]
        self.by_prefix: dict[tuple[JoinStep, ...], str] = {(): "t0"}
        self.entity_alias: dict[str, str] = {item.measure_entity: "t0"}
        self.joins: list[exp.Join] = []
        for entity_path in item.paths:
            for end in range(1, len(entity_path.steps) + 1):
                prefix = entity_path.steps[:end]
                if prefix not in self.by_prefix:
                    self.by_prefix[prefix] = self._join(model, prefix)
            self.entity_alias[entity_path.entity] = self.by_prefix[entity_path.steps]

    def _join(self, model: Model, prefix: tuple[JoinStep, ...]) -> str:
        step = prefix[-1]
        found = model.relationship(step.name)
        if found is None:  # planning resolved the path, so only a changed model gets here
            raise _failed("join step", step.name, "no such relationship")
        _, relationship = found
        left = self.by_prefix[prefix[:-1]]
        alias = _alias(len(self.by_prefix))
        on = exp.and_(
            *[
                exp.EQ(
                    this=exp.column(pair.from_, table=left, quoted=True),
                    expression=exp.column(pair.to, table=alias, quoted=True),
                )
                for pair in relationship.keys
            ]
        )
        self.joins.append(exp.Join(this=_table(model.entities[step.to], alias), side="LEFT", on=on))
        return alias

    def alias(self, entity: str) -> str:
        return self.entity_alias[entity]


def _dimension_expr(joins: _Joins, entity: str, dimension: Dimension) -> exp.Expr:
    return _bind(dimension.expr or dimension.name, joins.alias(entity))


def _input_query(model: Model, plan: QueryPlan, item: InputPlan) -> exp.Select:
    """The aggregate query for one simple metric (section 7.5)."""
    joins = _Joins(model, item)
    measure_entity = joins.measure_entity
    time_expr = _dimension_expr(joins, item.measure_entity, item.time_dimension)

    selects: list[exp.Expr] = []
    groups: list[exp.Expr] = []
    if plan.grain is not None:
        selects.append(exp.alias_(_trunc(plan.grain, time_expr.copy()), TIME_COLUMN, quoted=True))
        groups.append(_trunc(plan.grain, time_expr.copy()))
    for planned in plan.dimensions:
        value = _dimension_expr(joins, planned.entity, planned.dimension)
        selects.append(exp.alias_(value, planned.column, quoted=True))
        groups.append(value.copy())
    selects.append(exp.alias_(_aggregate(item), item.metric, quoted=True))

    where: list[exp.Expr] = []
    if item.measure.filter is not None:
        where.append(exp.paren(_bind(item.measure.filter, "t0")))
    if item.measure_rule is not None:
        where.append(exp.paren(_bind(item.measure_rule.expr, "t0")))
    for definition_filter in item.definition_filters:
        where.append(_definition_condition(joins, definition_filter))
    for request_filter in plan.filters:
        where.append(_filter_condition(joins, request_filter))
    in_range = _range(plan, time_expr)
    where.extend(in_range)
    if item.measure.semi_additive == "last":
        where.append(_last_snapshot(plan, measure_entity, item, time_expr))

    query = exp.select(*selects).from_(_table(measure_entity, "t0"))
    for join in joins.joins:
        query = query.join(join)
    if where:
        query = query.where(exp.and_(*where))
    if groups:
        query = query.group_by(*groups)
    return query


def _aggregate(item: InputPlan) -> exp.Expr:
    measure = item.measure
    argument = _bind(measure.expr, "t0") if measure.expr is not None else None
    if measure.agg == "count":
        return exp.Count(this=argument if argument is not None else exp.Star())
    if measure.agg == "count_distinct":
        return exp.Count(this=exp.Distinct(expressions=[_required(argument, item)]))
    return _PLAIN_AGGS[measure.agg](this=_required(argument, item))


def _required(argument: exp.Expr | None, item: InputPlan) -> exp.Expr:
    if argument is None:  # the definition model requires expr unless agg is count
        raise _failed("measure", item.measure.name, "its aggregate needs an expression")
    return argument


def _definition_condition(joins: _Joins, item: PlannedFilter | PlannedRule) -> exp.Expr:
    if isinstance(item, PlannedRule):
        return exp.paren(_bind(item.expr, joins.alias(item.entity)))
    return _filter_condition(joins, item)


def _filter_condition(joins: _Joins, item: PlannedFilter) -> exp.Expr:
    column = _dimension_expr(joins, item.entity, item.dimension)
    return _condition(column, item.op, item.value, item.dimension)


def _range(plan: QueryPlan, time_expr: exp.Expr) -> list[exp.Expr]:
    """The request's time range on one input's time dimension (start inclusive, end exclusive)."""
    found: list[exp.Expr] = []
    if plan.time_start is not None:
        found.append(exp.GTE(this=time_expr.copy(), expression=_date(plan.time_start)))
    if plan.time_end is not None:
        found.append(exp.LT(this=time_expr.copy(), expression=_date(plan.time_end)))
    return found


def _last_snapshot(
    plan: QueryPlan, entity: Entity, item: InputPlan, time_expr: exp.Expr
) -> exp.Expr:
    """Balances: keep only the latest snapshot date in each period of the time range."""
    inner_time = _bind(item.time_dimension.expr or item.time_dimension.name, "s0")
    latest = exp.select(exp.Max(this=inner_time.copy())).from_(_table(entity, "s0"))
    in_range = _range(plan, inner_time)
    if in_range:
        latest = latest.where(exp.and_(*in_range))
    if plan.grain is not None:
        latest = latest.group_by(_trunc(plan.grain, inner_time.copy()))
    return exp.In(this=time_expr.copy(), query=latest.subquery())


# --------------------------------------------------------------------------------------
# Ratio and derived metrics


def _combine(
    plan: QueryPlan,
    nodes: dict[str, MetricNode],
    root: MetricNode,
    keys: Sequence[str],
    leaves: dict[str, exp.Select],
) -> exp.Select:
    """Put each input in a CTE, `UNION ALL` them, `GROUP BY` the keys, then compute the metric."""
    names = [item.metric for item in plan.inputs]
    key_columns = [_name(k) for k in keys]

    union: exp.Query | None = None
    for name in names:
        part = exp.select(
            *[_name(k) for k in keys],
            *[_name(n) if n == name else exp.alias_(exp.Null(), n, quoted=True) for n in names],
        ).from_(exp.table_(_cte(name), quoted=True))
        union = part if union is None else exp.union(union, part, distinct=False)
    assert union is not None

    aggregates = {item.metric: item.measure.agg for item in plan.inputs}
    combined = exp.select(
        *[_name(k) for k in keys],
        *[exp.alias_(_merge(n, aggregates[n]), n, quoted=True) for n in names],
    ).from_(exp.table_("unioned", quoted=True))
    if keys:
        combined = combined.group_by(*[_name(k) for k in keys])

    final = exp.select(
        *key_columns,
        *[_shown(nodes, name) for name in [*root.inputs, root.name]],
    ).from_(exp.table_("combined", quoted=True))
    for name in names:
        final = final.with_(_cte_identifier(name), as_=leaves[name])
    return final.with_(_ident("unioned"), as_=union).with_(_ident("combined"), as_=combined)


def _shown(nodes: dict[str, MetricNode], name: str) -> exp.Expr:
    """A result column: an input read from the combined rows, or a metric computed from them."""
    value = _value(nodes, name)
    return value if isinstance(value, exp.Column) else exp.alias_(value, name, quoted=True)


def _merge(name: str, agg: str) -> exp.Expr:
    """One input's value for a key group: it appears once, so `MAX` picks it."""
    value = exp.Max(this=_name(name))
    if agg in _ZERO_WHEN_MISSING:
        return exp.Coalesce(this=value, expressions=[exp.Literal.number(0)])
    return value


def _value(nodes: dict[str, MetricNode], name: str) -> exp.Expr:
    """The expression that gives a metric's value from the combined columns."""
    node = nodes[name]
    if node.type == "simple":
        return _name(name)
    if node.type == "ratio":
        numerator, denominator = node.inputs
        return exp.Div(
            this=exp.Cast(this=_operand(nodes, numerator), to=exp.DataType.build("double")),
            expression=exp.Nullif(
                this=_operand(nodes, denominator), expression=exp.Literal.number(0)
            ),
        )
    return _derived(nodes, node)


def _operand(nodes: dict[str, MetricNode], name: str) -> exp.Expr:
    value = _value(nodes, name)
    return value if isinstance(value, exp.Column) else exp.paren(value)


def _derived(nodes: dict[str, MetricNode], node: MetricNode) -> exp.Expr:
    """A derived metric's expression, with each input name replaced by that input's value.

    A division needs no cast here: sqlglot prints `a / b` for PostgreSQL with a floating-point
    numerator, so integer inputs divide as they do on DuckDB (a test pins this).
    """
    assert node.expr is not None
    try:
        tree = _parsed(node.expr).copy()
    except SqlglotError as error:
        raise _failed("expression", node.expr, str(error).splitlines()[0]) from None
    inputs = set(node.inputs)

    def replace(found: exp.Expr) -> exp.Expr:
        if isinstance(found, exp.Column) and found.name in inputs:
            return _operand(nodes, found.name)
        return found

    return tree.transform(replace)
