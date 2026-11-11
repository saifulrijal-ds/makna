"""The explanation of a plan in plain sentences (ARCHITECTURE section 7.8, QRY-3).

`explain` reads a plan and the model it was planned against and returns two things: the ordered
sentences that say how the number is built, and the warnings a reader should not miss. The order
is fixed so that a reader (and a test) can rely on it:

1. the formula, and how the inputs are combined;
2. for each input, its time dimension and time range;
3. for each input, the filters and rules that are part of its definition;
4. the filters the request added;
5. the join path behind every dimension or filter that lives on another entity.

A balance metric adds a warning. Nothing here changes what the SQL does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from makna.compiler.graph import JoinStep
from makna.compiler.plan import InputPlan, PlannedFilter, PlannedRule, QueryPlan
from makna.compiler.snapshot import Model
from makna.model.definitions import Scalar

_OPS = {
    "=": "is",
    "!=": "is not",
    ">": "is greater than",
    ">=": "is at least",
    "<": "is less than",
    "<=": "is at most",
    "in": "is one of",
    "not_in": "is none of",
    "is_null": "is empty",
    "is_not_null": "is not empty",
}


@dataclass(frozen=True)
class Explanation:
    sentences: tuple[str, ...]
    warnings: tuple[str, ...]


def explain(model: Model, plan: QueryPlan) -> Explanation:
    sentences = [_formula(model, plan)]
    sentences.extend(_time_sentence(model, plan, item) for item in plan.inputs)
    for item in plan.inputs:
        sentences.extend(_definition_sentences(model, item))
    sentences.extend(_request_filter_sentences(plan))
    sentences.extend(_join_sentences(model, plan))
    warnings = [w for item in plan.inputs if (w := _balance_warning(model, plan, item))]
    return Explanation(tuple(sentences), tuple(warnings))


# --------------------------------------------------------------------------------------
# Wording helpers


def _label(model: Model, metric: str) -> str:
    found = model.metrics.get(metric)
    return found.label if found is not None else metric


def _entity_label(model: Model, entity: str) -> str:
    found = model.entities.get(entity)
    return found.label if found is not None else entity


def _words(name: str) -> str:
    return name.replace("_", " ")


def _list(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _value(value: Scalar | tuple[Scalar, ...] | None) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple):
        return ", ".join(_value(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value) if isinstance(value, str) else str(value)


def _named(model: Model, ref: str) -> str:
    """A dimension as a reader names it, with its reference: `Region (branch.region_name)`."""
    found = model.dimension(ref)
    return f"{found[1].label or _words(found[1].name)} ({ref})" if found is not None else ref


def _sentence(text: str) -> str:
    return text if text.endswith((".", "?", "!")) else text + "."


# --------------------------------------------------------------------------------------
# 1. The formula


def _key_phrase(model: Model, plan: QueryPlan) -> str:
    parts = [plan.grain] if plan.grain is not None else []
    parts.extend(d.dimension.label or _words(d.name) for d in plan.dimensions)
    return _list([p.lower() if i == 0 and plan.grain else p for i, p in enumerate(parts)])


def _formula(model: Model, plan: QueryPlan) -> str:
    root = next(n for n in plan.nodes if n.name == plan.metric)
    label = _label(model, root.name)
    keys = _key_phrase(model, plan)
    combined = f"combined by {keys}" if keys else "combined into one value"
    if root.type == "ratio":
        numerator, denominator = root.inputs
        return (
            f"{label} = {_label(model, numerator)} divided by {_label(model, denominator)}. "
            f"Each side is calculated separately and then {combined}; a zero denominator "
            "gives no value (empty), not an error."
        )
    if root.type == "derived":
        names = _list([_label(model, n) for n in root.inputs])
        return (
            f"{label} = {root.expr}, computed from {names}. Each input is calculated "
            f"separately and then {combined}."
        )
    item = plan.inputs[0]
    return f"{label} is {_aggregate_phrase(item)} of {_entity_label(model, item.measure_entity)}."


def _aggregate_phrase(item: InputPlan) -> str:
    measure, expr = item.measure, item.measure.expr
    phrases = {
        "sum": f"the sum of {expr}",
        "avg": f"the average of {expr}",
        "min": f"the lowest {expr}",
        "max": f"the highest {expr}",
        "count": "the number of rows" if expr is None else f"the number of rows with {expr}",
        "count_distinct": f"the number of distinct values of {expr}",
    }
    return phrases[measure.agg]


# --------------------------------------------------------------------------------------
# 2. Time


def _time_sentence(model: Model, plan: QueryPlan, item: InputPlan) -> str:
    label = _label(model, item.metric)
    entity = _entity_label(model, item.measure_entity)
    time = item.time_dimension.label or _words(item.time_dimension.name)
    head = f"{label}: {entity} rows are placed by {time} ({item.measure_entity}."
    head += f"{item.time_dimension.name})"
    if plan.time_start is not None and plan.time_end is not None:
        window = f", from {plan.time_start} up to but not including {plan.time_end}"
    elif plan.time_start is not None:
        window = f", from {plan.time_start} onward"
    elif plan.time_end is not None:
        window = f", up to but not including {plan.time_end}"
    else:
        window = ", with no limit on time"
    by = f", grouped by {plan.grain}" if plan.grain is not None else ""
    return _sentence(head + window + by)


# --------------------------------------------------------------------------------------
# 3 and 4. Filters


def _definition_sentences(model: Model, item: InputPlan) -> list[str]:
    label = _label(model, item.metric)
    found: list[str] = []
    if item.measure.filter is not None:
        found.append(
            f"{label}: only rows where {item.measure.filter} count (part of the measure definition)"
        )
    if item.measure_rule is not None:
        found.append(
            f"{label}: only rows where {_rule(item.measure_rule)} count (part of the measure)"
        )
    for each in item.definition_filters:
        what = _rule(each) if isinstance(each, PlannedRule) else _filter(each)
        found.append(f"{label}: only rows where {what} count (part of the metric definition)")
    return [_sentence(text) for text in found]


def _rule(rule: PlannedRule) -> str:
    return f"the rule {rule.ref} holds ({rule.expr}; {_sentence(rule.description).rstrip('.')})"


def _filter(item: PlannedFilter) -> str:
    return f"{item.ref} {_OPS[item.op]} {_value(item.value)}".strip()


def _request_filter_sentences(plan: QueryPlan) -> list[str]:
    return [_sentence(f"The request keeps only rows where {_filter(f)}") for f in plan.filters]


# --------------------------------------------------------------------------------------
# 5. Join paths


def _join_sentences(model: Model, plan: QueryPlan) -> list[str]:
    """One sentence for each entity a dimension or filter reads, and each distinct way its
    inputs reach it. Dimensions and filters on an input's own table need no join."""
    uses: dict[str, list[str]] = {}
    for dimension in plan.dimensions:
        uses.setdefault(dimension.entity, []).append(dimension.ref)
    for each in plan.filters:
        uses.setdefault(each.entity, []).append(each.ref)
    found: list[str] = []
    for entity, refs in uses.items():
        routes: dict[tuple[JoinStep, ...], list[str]] = {}
        for item in plan.inputs:
            path = next((p.steps for p in item.paths if p.entity == entity), None)
            if path:
                routes.setdefault(path, []).append(item.metric)
        for steps, metrics in routes.items():
            found.append(_route(model, entity, list(dict.fromkeys(refs)), steps, metrics))
    return found


def _route(
    model: Model, entity: str, refs: list[str], steps: tuple[JoinStep, ...], metrics: list[str]
) -> str:
    who = _list([_label(model, m) for m in metrics])
    chain = ", then ".join(step.name for step in steps)
    names = " > ".join([steps[0].entity, *[step.to for step in steps]])
    last = model.relationship(steps[-1].name)
    why = f" ({_sentence(last[1].description).rstrip('.')})" if last is not None else ""
    shown = [_named(model, ref) for ref in refs]
    verb = "comes" if len(shown) == 1 else "come"
    return _sentence(
        f"{_list(shown)} {verb} from {_entity_label(model, entity)}: for {who}, rows are joined "
        f"{names} through {chain}{why}"
    )


# --------------------------------------------------------------------------------------
# Warnings


def _balance_warning(model: Model, plan: QueryPlan, item: InputPlan) -> str | None:
    if item.measure.semi_additive != "last":
        return None
    label = _label(model, item.metric)
    if plan.grain is not None:
        return (
            f"{label} is a balance: the value for each {plan.grain} is the balance at its last "
            "snapshot date, and balances are never added up across dates."
        )
    return (
        f"{label} is a balance: the value is the balance at the latest snapshot date in the "
        "time range, and balances are never added up across dates."
    )
