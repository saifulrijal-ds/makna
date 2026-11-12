"""The explanation of a plan in plain sentences (ARCHITECTURE section 7.8, QRY-3).

`explain` reads a plan and the model it was planned against and returns two things: the ordered
sentences that say how the number is built, and the warnings a reader should not miss. The order
is fixed so that a reader (and a test) can rely on it:

1. the formula, and how the inputs are combined;
2. for each input, its time dimension and time range;
3. for each input, the filters and rules that are part of its definition;
4. the filters the request added;
5. the join path behind every dimension or filter that lives on another entity;
6. for each masked personal dimension, that it is masked and how;
7. when the governance policy relaxes a rule (D-29), one sentence that names the mode and each
   relaxed rule.

A balance metric adds a warning, and so does a personal dimension while the policy has a minimum
group size: groups built from fewer than the minimum number of records are left out, and the
result column that says how many is named. A relaxed policy adds one more warning that says the
same as sentence 7. Nothing here changes what the SQL does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from makna.compiler.governance import GROUPS_LEFT_OUT
from makna.compiler.graph import JoinStep
from makna.compiler.plan import InputPlan, PlannedDimension, PlannedFilter, PlannedRule, QueryPlan
from makna.compiler.snapshot import Model
from makna.model.definitions import Scalar
from makna.model.governance import GovernancePolicy

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
    sentences.extend(_mask_sentence(model, d) for d in plan.dimensions if d.masked)
    relaxed = relaxed_rules_text(plan.governance)
    if relaxed is not None:
        sentences.append(_relaxed_sentence(plan.governance, relaxed))
    warnings = [w for item in plan.inputs if (w := _balance_warning(model, plan, item))]
    if plan.guard:
        warnings.append(_group_size_warning(plan))
    if relaxed is not None:
        warnings.append(_relaxed_warning(plan.governance, relaxed))
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
    """A dimension as a reader names it, with its reference: `Region (branch.region_name)`.
    A rule has no dimension and is named by its reference."""
    found = model.dimension(ref)
    if found is None:  # not a dimension, so a rule of the metric definition
        return f"rule {ref}"
    return f"{found[1].label or _words(found[1].name)} ({ref})"


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
    """One sentence for each entity that a dimension, a request filter, or a rule or filter of
    the metric definition reads, and each distinct way its inputs reach it. Things on an input's
    own table need no join."""
    requested: dict[str, list[str]] = {}
    for dimension in plan.dimensions:
        requested.setdefault(dimension.entity, []).append(dimension.ref)
    for each in plan.filters:
        requested.setdefault(each.entity, []).append(each.ref)
    groups: dict[tuple[str, tuple[JoinStep, ...]], tuple[list[str], list[str]]] = {}
    for item in plan.inputs:
        defined: dict[str, list[str]] = {}
        for definition in item.definition_filters:
            defined.setdefault(definition.entity, []).append(definition.ref)
        for entity_path in item.paths:
            refs = requested.get(entity_path.entity, []) + defined.get(entity_path.entity, [])
            if not entity_path.steps or not refs:
                continue
            seen, who = groups.setdefault((entity_path.entity, entity_path.steps), ([], []))
            seen.extend(refs)
            who.append(item.metric)
    return [
        _route(model, entity, list(dict.fromkeys(refs)), steps, metrics)
        for (entity, steps), (refs, metrics) in groups.items()
    ]


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
    text = (
        f"{_list(shown)} {verb} from {_entity_label(model, entity)}: for {who}, rows are joined "
        f"{names} through {chain}{why}"
    )
    return _sentence(text[0].upper() + text[1:])


# --------------------------------------------------------------------------------------
# 6. Masks


def _mask_sentence(model: Model, planned: PlannedDimension) -> str:
    masking = planned.dimension.masking
    assert masking is not None  # planning refuses a personal dimension without a mask
    if masking.method == "redact":
        how = "it is replaced by *** in every row"
    elif masking.method == "truncate_date":
        how = f"the date is cut to the first day of its {masking.grain}"
    else:
        how = (
            f"it is shown as the lower bound of its band, and every band is {masking.width:g} wide"
        )
    text = (
        f"{_named(model, planned.ref)} is personal data, so it is masked: {how} ({masking.method})"
    )
    return _sentence(text[0].upper() + text[1:])


# --------------------------------------------------------------------------------------
# Warnings


_RELAXED_MEANING = {
    "min_group_size": "no group is left out for being small",
    "allow_personal_filters": "filters on personal dimensions are allowed",
    "apply_masks": "masked personal dimensions are returned unmasked",
    "allow_unmasked_personal": "personal dimensions without a masking rule are returned unmasked",
}


def relaxed_rules_text(policy: GovernancePolicy) -> str | None:
    """The relaxed rules with their values, such as `min_group_size = 0 (no group is left out for
    being small) and apply_masks = false (...)`, or None when the policy relaxes nothing."""
    relaxed = policy.relaxed()
    if not relaxed:
        return None
    return _list([f"{n} = {str(v).lower()} ({_RELAXED_MEANING[n]})" for n, v in relaxed])


def _relaxed_sentence(policy: GovernancePolicy, rules: str) -> str:
    return _sentence(
        f"This server runs in governance mode {policy.mode}, which relaxes {rules}, so the "
        "personal-data protection that applies by default is reduced for this result"
    )


def _relaxed_warning(policy: GovernancePolicy, rules: str) -> str:
    return _sentence(
        f"Governance mode {policy.mode} relaxes {rules}. The server configuration sets this, not "
        "the request. Personal-data protection is reduced: check that the people and agents who "
        "can read this result may see the data it can contain"
    )


def _group_size_warning(plan: QueryPlan) -> str:
    records = _list([f"{g.label} records" for g in plan.guard])
    return (
        f"Groups built from fewer than {plan.min_group_size} {records} are left out of this "
        "result to protect personal data. This does not stop someone from comparing two "
        "requests that differ by one person. The number of groups left out "
        f"is in the result column {GROUPS_LEFT_OUT}, the same on every row. A result with no rows "
        "can mean that every group was left out."
    )


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
