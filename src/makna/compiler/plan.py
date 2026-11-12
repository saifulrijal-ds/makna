"""Request planning (ARCHITECTURE sections 7.1 to 7.4): everything decided before SQL is written.

`plan_request` is a pure function of a model snapshot and a request. It checks the metric (known,
approved, no open reference issue), expands it to its simple inputs, checks the time range, limit,
and grain, finds the join path from each input's entity to every entity a dimension, filter, or
rule needs, and checks filter values. The result is a frozen `QueryPlan`; SQL generation (WP-09)
reads it and decides nothing new. Any failure is one `QueryError` and nothing is planned.

`available_dimensions` is the same logic seen from the other side: which dimensions a metric
can be grouped or filtered by (section 7.4). The indexer stores its result.

Personal data (WP-10, WP-10B): the planner applies the `GovernancePolicy` it is given (D-29). A
personal dimension is refused, masked, or returned raw as the policy says, a filter on one is
refused or planned as the policy says, and the record counts for the minimum group size are added
only when the policy has one. Under the default `strict` policy nothing is relaxed.
"""

from __future__ import annotations

import datetime
import difflib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, get_args

from makna.compiler import governance as personal_data
from makna.compiler.graph import JoinPath, JoinStep, path_names, resolve_path
from makna.compiler.snapshot import Model
from makna.model.definitions import Dimension, Entity, Grain, Measure, Metric, RuleRef, Scalar
from makna.model.definitions import Filter as DefinitionFilter
from makna.model.governance import STRICT, GovernancePolicy
from makna.model.request import (
    FILTER_OPS,
    MAX_LIMIT,
    ErrorCode,
    QueryError,
    QueryRequest,
    RequestFilter,
    ResultColumn,
)

GRAINS: tuple[str, ...] = get_args(Grain)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ORDERING_OPS = (">", ">=", "<", "<=")


# --------------------------------------------------------------------------------------
# Results


@dataclass(frozen=True)
class InputPath:
    """The join path one simple input takes to an entity (empty when it is the measure's own)."""

    metric: str
    steps: tuple[str, ...]


@dataclass(frozen=True)
class DimensionOption:
    """One dimension a metric offers (section 5.4). `available` is False for a personal dimension
    that has no masking rule: it is listed with the reason so that agents do not try it."""

    ref: str
    entity: str
    name: str
    description: str
    type: str
    sensitivity: str
    masking: str | None
    available: bool
    reason: str | None
    paths: tuple[InputPath, ...]


ExcludedKind = Literal["no_path", "ambiguous", "inconsistent"]


@dataclass(frozen=True)
class ExcludedEntity:
    """An entity whose dimensions the metric cannot offer, and why.

    `kind` is `no_path`, `ambiguous`, or `inconsistent` (the inputs of a ratio or derived metric
    enter the entity through different relationships).
    """

    entity: str
    kind: ExcludedKind
    reason: str
    candidate_paths: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class AvailableDimensions:
    options: tuple[DimensionOption, ...]
    excluded: tuple[ExcludedEntity, ...]

    def refs(self) -> tuple[str, ...]:
        """The dimensions that can be used, as `entity.dimension`."""
        return tuple(o.ref for o in self.options if o.available)

    @property
    def warnings(self) -> tuple[str, ...]:
        """What curators should fix: entities left out because a join choice is missing."""
        return tuple(e.reason for e in self.excluded if e.kind in ("ambiguous", "inconsistent"))


@dataclass(frozen=True)
class MetricNode:
    """One metric of the expansion. `inputs` are its direct inputs (none for a simple metric)."""

    name: str
    type: str
    inputs: tuple[str, ...]
    expr: str | None
    unit: str | None


@dataclass(frozen=True)
class PlannedDimension:
    ref: str
    entity: str
    name: str
    dimension: Dimension
    sensitivity: str
    masked: bool = False
    """True when SQL generation puts the dimension's masking rule around it: a personal dimension
    that has a rule, while the policy applies masks. A personal dimension that is not masked is
    returned raw, which the policy must allow."""

    @property
    def column(self) -> str:
        return f"{self.entity}__{self.name}"

    @property
    def personal(self) -> bool:
        return self.sensitivity == "personal"


@dataclass(frozen=True)
class GuardedEntity:
    """An entity that holds a selected personal dimension. Every input counts its distinct
    records, in the column `column`, so that small groups can be left out (section 7.6)."""

    entity: str
    label: str
    primary_key: tuple[str, ...]
    column: str


@dataclass(frozen=True)
class PlannedFilter:
    """A structured filter, with the value checked and lists made tuples."""

    ref: str
    entity: str
    op: str
    value: Scalar | tuple[Scalar, ...] | None
    dimension: Dimension
    sensitivity: str


@dataclass(frozen=True)
class PlannedRule:
    """A named rule used by a measure or a metric filter (D-17)."""

    ref: str
    entity: str
    name: str
    expr: str
    description: str


@dataclass(frozen=True)
class EntityPath:
    entity: str
    steps: JoinPath


@dataclass(frozen=True)
class InputPlan:
    """What SQL generation needs for one simple metric: its measure, the rules and filters that
    belong to its definition, and a join path for every other entity it reads."""

    metric: str
    measure_entity: str
    measure: Measure
    time_dimension: Dimension
    measure_rule: PlannedRule | None
    definition_filters: tuple[PlannedFilter | PlannedRule, ...]
    paths: tuple[EntityPath, ...]

    @property
    def join_steps(self) -> tuple[JoinStep, ...]:
        """The distinct joins, in the order they are needed (a shared prefix is joined once)."""
        seen: dict[JoinStep, None] = {}
        for entity_path in self.paths:
            for step in entity_path.steps:
                seen.setdefault(step, None)
        return tuple(seen)


@dataclass(frozen=True)
class QueryPlan:
    metric: str
    version: int | None
    nodes: tuple[MetricNode, ...]
    inputs: tuple[InputPlan, ...]
    dimensions: tuple[PlannedDimension, ...]
    filters: tuple[PlannedFilter, ...]
    grain: str | None
    time_start: datetime.date | None
    time_end: datetime.date | None
    limit: int
    columns: tuple[ResultColumn, ...]
    governance: GovernancePolicy = STRICT
    guard: tuple[GuardedEntity, ...] = ()
    """Empty unless a personal dimension is selected and the policy has a minimum group size."""
    personal_columns: frozenset[tuple[str, str]] = frozenset()
    """`(table, column)` read by any personal dimension of the model, for `governance.verify`."""
    raw_columns: frozenset[tuple[str, str]] = frozenset()
    """The personal columns the policy lets the select list and GROUP BY return unmasked, for
    `governance.verify`. Empty under `strict`."""

    @property
    def min_group_size(self) -> int:
        return self.governance.min_group_size


# --------------------------------------------------------------------------------------
# Errors


def _broken(*problems: tuple[str, str, str]) -> QueryError:
    """BROKEN_REFERENCE from (code, object, message) triples."""
    issues = [{"code": c, "object": o, "message": m} for c, o, m in problems]
    return QueryError(
        ErrorCode.BROKEN_REFERENCE,
        "The definitions this metric depends on are broken: "
        + "; ".join(m for _, _, m in problems)
        + ". A curator must fix them before the metric can be used.",
        {"issues": issues},
    )


def _unresolved(metric: str, message: str) -> QueryError:
    return _broken(("UNRESOLVED", f"metric:{metric}", message))


def _in_metric(metric: str, error: QueryError) -> QueryError:
    return QueryError(
        error.code, f"Metric {metric}: {error.message}", {**error.details, "metric": metric}
    )


# --------------------------------------------------------------------------------------
# Expanding a metric


def _direct_inputs(metric: Metric) -> tuple[str, ...]:
    if metric.type == "ratio":
        return tuple(n for n in (metric.numerator, metric.denominator) if n is not None)
    if metric.type == "derived":
        return tuple(metric.inputs or ())
    return ()


def _closure(model: Model, root: Metric) -> list[Metric]:
    """The metric and everything it depends on, inputs first, the metric last."""
    order: list[Metric] = []
    done: set[str] = set()

    def visit(metric: Metric, trail: tuple[str, ...]) -> None:
        if metric.name in done:
            return
        if metric.name in trail:
            raise _unresolved(root.name, f"metric {metric.name} depends on itself")
        for name in _direct_inputs(metric):
            child = model.metrics.get(name)
            if child is None:
                raise _unresolved(root.name, f"{metric.name} uses {name}, which is not a metric")
            visit(child, (*trail, metric.name))
        done.add(metric.name)
        order.append(metric)

    visit(root, ())
    return order


def _leaves(model: Model, root: Metric) -> list[Metric]:
    return [m for m in _closure(model, root) if m.type == "simple"]


def _leaf_measure(model: Model, leaf: Metric) -> tuple[Entity, Measure]:
    found = model.measure(leaf.measure or "")
    if found is None:
        raise _unresolved(leaf.name, f"measure {leaf.measure} does not exist")
    return found


def _unit(metric: Metric) -> str | None:
    return metric.format.unit or ("percent" if metric.format.kind == "percent" else None)


def allowed_grains(model: Model, metric: Metric) -> tuple[str, ...]:
    """The grains the metric offers: its `time_grains` (all if unset) and those of its inputs."""
    allowed = set(GRAINS)
    for member in _closure(model, metric):
        allowed &= set(member.time_grains or GRAINS)
    return tuple(g for g in GRAINS if g in allowed)


# --------------------------------------------------------------------------------------
# Available dimensions (section 7.4)


def available_dimensions(
    model: Model, metric: Metric, *, governance: GovernancePolicy = STRICT
) -> AvailableDimensions:
    """The dimensions `metric` can be grouped or filtered by.

    Simple metric: the dimensions of the measure's entity and of every entity it reaches by the
    path rule. Ratio or derived metric: of an entity every simple input reaches, and only when
    all inputs enter it through the same final relationship. Entities that need a join choice the
    metric does not make are left out and reported in `excluded` (and `warnings`).

    The result depends on `governance` (D-29): a personal dimension without a masking rule is
    listed as not available unless the policy allows unmasked personal data. Under
    `apply_masks = false` a masked dimension is listed as available but returns the raw value of
    its column (`customer.birth_year` returns full dates), which can be much finer than its name.

    Raises `BROKEN_REFERENCE` if the metric's inputs or measures do not resolve.
    """
    leaves = [(leaf, _leaf_measure(model, leaf)[0].name) for leaf in _leaves(model, metric)]
    options: list[DimensionOption] = []
    excluded: list[ExcludedEntity] = []
    for entity in model.entities.values():
        paths: list[InputPath] = []
        failures: list[tuple[Metric, QueryError]] = []
        for leaf, start in leaves:
            try:
                path = resolve_path(model, start, entity.name, leaf.join_paths)
            except QueryError as error:
                failures.append((leaf, error))
                continue
            paths.append(InputPath(leaf.name, tuple(path_names(path))))
        if failures:
            excluded.append(_exclusion(entity.name, failures))
            continue
        if len({p.steps[-1] if p.steps else None for p in paths}) > 1:
            excluded.append(_inconsistent(entity.name, paths))
            continue
        options.extend(_options(model, entity, tuple(paths), governance))
    return AvailableDimensions(tuple(options), tuple(excluded))


def _show(paths: Sequence[Sequence[str]]) -> str:
    return ", ".join(" > ".join(p) for p in paths)


def _exclusion(entity: str, failures: list[tuple[Metric, QueryError]]) -> ExcludedEntity:
    for leaf, error in failures:
        if error.code == ErrorCode.AMBIGUOUS_JOIN_PATH:
            candidates = tuple(tuple(p) for p in error.details["candidate_paths"])
            return ExcludedEntity(
                entity,
                "ambiguous",
                f"{entity} dimensions are left out because {leaf.name} reaches {entity} by "
                f"{len(candidates)} equally short paths ({_show(candidates)}); "
                "pin one in join_paths",
                candidates,
            )
    for leaf, error in failures:
        if "pinned" in error.details:
            return ExcludedEntity(entity, "no_path", f"{leaf.name}: {error.message}")
    names = ", ".join(leaf.name for leaf, _ in failures)
    return ExcludedEntity(entity, "no_path", f"no approved join path from {names} to {entity}")


def _inconsistent(entity: str, paths: list[InputPath]) -> ExcludedEntity:
    entries = ", ".join(
        f"{p.metric} via {p.steps[-1] if p.steps else 'its own table'}" for p in paths
    )
    own = [p.metric for p in paths if not p.steps]
    if own:
        advice = (
            f"{', '.join(own)} reads {entity} itself, so no pin can make the inputs share "
            f"its dimensions"
        )
    else:
        advice = (
            f"a pin helps only if each input can reach {entity} by a chain that ends in the "
            "same relationship; pin such a chain in every input if such a chain exists"
        )
    return ExcludedEntity(
        entity,
        "inconsistent",
        f"{entity} dimensions are left out because the inputs enter {entity} through different "
        f"relationships ({entries}), so the result would divide unlike numbers; {advice}",
        tuple(p.steps for p in paths),
    )


def _options(
    model: Model, entity: Entity, paths: tuple[InputPath, ...], governance: GovernancePolicy
) -> list[DimensionOption]:
    found: list[DimensionOption] = []
    for dimension in entity.dimensions:
        sensitivity = model.effective_sensitivity(entity, dimension)
        unmasked = (
            sensitivity == "personal"
            and dimension.masking is None
            and not governance.allow_unmasked_personal
        )
        found.append(
            DimensionOption(
                ref=f"{entity.name}.{dimension.name}",
                entity=entity.name,
                name=dimension.name,
                description=dimension.description,
                type=dimension.type,
                sensitivity=sensitivity,
                masking=dimension.masking.method if dimension.masking is not None else None,
                available=not unmasked,
                reason="personal data without a masking rule cannot be returned"
                if unmasked
                else None,
                paths=paths,
            )
        )
    return found


# --------------------------------------------------------------------------------------
# Planning


def plan_request(
    model: Model,
    request: QueryRequest,
    *,
    enforce_status: bool = True,
    governance: GovernancePolicy = STRICT,
) -> QueryPlan:
    """Plan `request` against `model`, or raise one `QueryError`.

    `enforce_status=False` skips the check that the metric and its inputs are approved. It is for
    the example-request check (V19), which runs before a metric is approved.

    `governance` is the policy of the deployment (D-29), built from the `[governance]` setting
    of `config/makna.toml`. The compiler does not read configuration; the caller passes it. The
    default is `strict` with a minimum group size of 10.
    """
    metric = model.metrics.get(request.metric)
    if metric is None:
        raise _unknown_metric(model, request.metric)
    closure = _closure(model, metric)
    if enforce_status:
        _check_status(closure)
    leaves = [m for m in closure if m.type == "simple"]
    measures = {leaf.name: _leaf_measure(model, leaf) for leaf in leaves}
    _check_references(model, {e.name for e, _ in measures.values()} | _definition_entities(leaves))

    _check_limit(request.limit)
    start, end = _time_range(request.time_start, request.time_end)
    _check_grain(model, metric, request.grain)
    _check_official_dates(model, request, [measures[leaf.name] for leaf in leaves], governance)

    available = available_dimensions(model, metric, governance=governance)
    dimensions = tuple(
        _planned_dimension(model, available, ref, governance)
        for ref in dict.fromkeys(request.dimensions)
    )
    filters = tuple(_planned_filter(model, available, f, governance) for f in request.filters)

    date_entities = _personal_date_entities(
        model, request, [measures[leaf.name] for leaf in leaves]
    )
    guard = _guard(model, dimensions, governance, date_entities)
    _check_guardable_dates(model, guard, date_entities)
    inputs = tuple(
        _plan_input(model, leaf, *measures[leaf.name], dimensions, filters, guard)
        for leaf in leaves
    )
    touched = {i.measure_entity for i in inputs}
    for plan_input in inputs:
        for entity_path in plan_input.paths:
            touched.add(entity_path.entity)
            touched.update(n for step in entity_path.steps for n in (step.entity, step.to))
    _check_references(model, touched)
    return QueryPlan(
        metric=metric.name,
        version=metric.version,
        nodes=tuple(
            MetricNode(m.name, m.type, _direct_inputs(m), m.expr, _unit(m)) for m in closure
        ),
        inputs=inputs,
        dimensions=dimensions,
        filters=filters,
        grain=request.grain,
        time_start=start,
        time_end=end,
        limit=request.limit,
        columns=_columns(metric, closure, request.grain, dimensions, guard),
        governance=governance,
        guard=guard,
        personal_columns=personal_data.personal_columns(model),
        raw_columns=personal_data.raw_returnable_columns(model, governance),
    )


def _unknown_metric(model: Model, name: str) -> QueryError:
    close = difflib.get_close_matches(name, list(model.metrics), n=3, cutoff=0.5)
    hint = f" Did you mean {', '.join(close)}?" if close else ""
    return QueryError(
        ErrorCode.UNKNOWN_METRIC,
        f"There is no metric named {name}.{hint} Search for the metric and use its name exactly.",
        {"metric": name, "close_names": close},
    )


def _check_status(closure: list[Metric]) -> None:
    for member in reversed(closure):  # the requested metric first, then its inputs
        who = f"Metric {member.name}"
        if member.status in ("draft", "in_review"):
            raise QueryError(
                ErrorCode.NOT_APPROVED,
                f"{who} is {member.status.replace('_', ' ')} and cannot be used until it is "
                "approved. Choose an approved metric.",
                {"metric": member.name, "status": member.status},
            )
        if member.status == "deprecated":
            reason = member.deprecation.reason if member.deprecation else None
            replaced_by = member.deprecation.replaced_by if member.deprecation else None
            use = f" Use {replaced_by} instead." if replaced_by else ""
            raise QueryError(
                ErrorCode.DEPRECATED,
                f"{who} is deprecated.{use}",
                {"metric": member.name, "reason": reason, "replaced_by": replaced_by},
            )


def _definition_entities(leaves: list[Metric]) -> set[str]:
    """Entities named by the measures, filters, and rules of the simple metrics."""
    names: set[str] = set()
    for leaf in leaves:
        names.add((leaf.measure or "").partition(".")[0])
        for item in leaf.filters or []:
            ref = item.rule if isinstance(item, RuleRef) else item.dimension
            names.add(ref.partition(".")[0])
    return names


def _check_references(model: Model, entities: set[str]) -> None:
    problems = [
        (issue.code, f"entity:{name}", issue.message)
        for name in sorted(entities)
        for issue in model.reference_issues.get(name, ())
    ]
    if problems:
        raise _broken(*problems)


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise QueryError(
            ErrorCode.LIMIT_EXCEEDED,
            f"limit must be between 1 and {MAX_LIMIT}, not {limit}. Ask for fewer rows or "
            "narrow the request with filters.",
            {"limit": limit, "max_limit": MAX_LIMIT},
        )


def _date(text: str | None, field: str) -> datetime.date | None:
    if text is None:
        return None
    try:
        if not _DATE.fullmatch(text):
            raise ValueError(text)
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise QueryError(
            ErrorCode.INVALID_TIME_RANGE,
            f"{field} must be an ISO date such as 2026-07-01, not {text!r}. Resolve words like "
            "'last month' to dates before calling.",
            {"field": field, "value": text},
        ) from None


def _time_range(
    time_start: str | None, time_end: str | None
) -> tuple[datetime.date | None, datetime.date | None]:
    start, end = _date(time_start, "time_start"), _date(time_end, "time_end")
    if start is not None and end is not None and start >= end:
        raise QueryError(
            ErrorCode.INVALID_TIME_RANGE,
            f"time_start ({start}) must be before time_end ({end}); time_start is inclusive and "
            "time_end is exclusive.",
            {"time_start": time_start, "time_end": time_end},
        )
    return start, end


def _check_official_dates(
    model: Model,
    request: QueryRequest,
    measures: list[tuple[Entity, Measure]],
    governance: GovernancePolicy,
) -> None:
    """PERSONAL_DATA_REFUSED when an input's official date is personal and the request would use
    it in a way the policy does not allow. A time range, and a balance (which always keeps the
    month-end rows of its snapshot date), filter on the date. A grain selects the date. A
    personal date cannot be returned or filtered on under `strict`, and a catalog that starts
    tagging a date personal closes this door at once (fail closed; section 7.6). The policy
    decides as it does for a dimension: filtering needs `allow_personal_filters`, and selecting
    needs the date to be returnable raw (`apply_masks` false if it has a masking rule,
    `allow_unmasked_personal` if not)."""
    ranged = bool(request.time_start or request.time_end)
    for entity, measure in measures:
        date = next((d for d in entity.dimensions if d.name == measure.time_dimension), None)
        if date is None or model.effective_sensitivity(entity, date) != "personal":
            continue
        filtered = ranged or measure.semi_additive == "last"
        selected = request.grain is not None
        if (filtered and not governance.allow_personal_filters) or (
            selected and not governance.may_return_raw(date.masking is not None)
        ):
            ref = f"{entity.name}.{date.name}"
            message = (
                f"The official date of {measure.name} ({ref}) is personal data, so this metric "
                "cannot be grouped by time or limited to a time range. Ask without grain, "
                "time_start, and time_end, or choose another metric."
            )
            if governance.mode != "strict":
                message = _date_refusal(
                    measure.name,
                    ref,
                    governance,
                    needs_filter=filtered and not governance.allow_personal_filters,
                    needs_raw=selected and not governance.may_return_raw(date.masking is not None),
                    has_mask=date.masking is not None,
                )
            raise QueryError(
                ErrorCode.PERSONAL_DATA_REFUSED,
                message,
                {
                    "dimension": ref,
                    "use": "time",
                    "masked_form_available": date.masking is not None,
                    "available_dimensions": [],
                },
            )


def _date_refusal(
    measure: str,
    ref: str,
    governance: GovernancePolicy,
    *,
    needs_filter: bool,
    needs_raw: bool,
    has_mask: bool,
) -> str:
    """The refusal for a personal official date under a policy that is not `strict`: it says
    which use is refused and which rule would allow it."""
    parts = []
    if needs_filter:
        parts.append(
            "a time range, or the month-end selection of a balance, filters on it "
            "(the rule allow_personal_filters would allow that)"
        )
    if needs_raw:
        rule = "apply_masks = false" if has_mask else "allow_unmasked_personal"
        parts.append(f"a grain returns it (the rule {rule} would allow that)")
    return (
        f"The official date of {measure} ({ref}) is personal data, and under the governance mode "
        f"{governance.mode} of this server {' and '.join(parts)}. The server configuration, not "
        "the request, sets the rules. Ask without what is refused, or choose another metric."
    )


def _check_grain(model: Model, metric: Metric, grain: str | None) -> None:
    if grain is None:
        return
    allowed = allowed_grains(model, metric)
    if grain not in allowed:
        raise QueryError(
            ErrorCode.INVALID_GRAIN,
            f"{metric.name} cannot be grouped by {grain}. Use one of: {', '.join(allowed)}, "
            "or leave grain out.",
            {"grain": grain, "allowed_grains": list(allowed)},
        )


# ---- dimensions and filters -----------------------------------------------------------


def _find(model: Model, available: AvailableDimensions, ref: str) -> tuple[Entity, Dimension]:
    """The dimension `ref`, or UNKNOWN_DIMENSION."""
    found = model.dimension(ref)
    if found is None:
        raise QueryError(
            ErrorCode.UNKNOWN_DIMENSION,
            f"There is no dimension {ref}. Use one of the available dimensions, written "
            "entity.dimension.",
            {"dimension": ref, "available_dimensions": list(available.refs())},
        )
    return found


def _check_reachable(available: AvailableDimensions, ref: str, entity: Entity) -> None:
    """DIMENSION_NOT_AVAILABLE if the metric cannot reach the dimension's entity."""
    problem = next((e for e in available.excluded if e.entity == entity.name), None)
    if problem is not None:
        raise QueryError(
            ErrorCode.DIMENSION_NOT_AVAILABLE,
            f"This metric cannot be grouped or filtered by {ref}: {problem.reason}. "
            "Choose one of the available dimensions.",
            {
                "dimension": ref,
                "reason": problem.reason,
                "available_dimensions": list(available.refs()),
                "candidate_paths": [list(c) for c in problem.candidate_paths],
            },
        )


def _refuse_personal(
    available: AvailableDimensions,
    ref: str,
    dimension: Dimension,
    sensitivity: str,
    use: Literal["group", "filter"],
    governance: GovernancePolicy,
) -> None:
    """PERSONAL_DATA_REFUSED (section 7.6) for a personal dimension that is filtered on, or
    grouped by without a masking rule, unless the policy allows it (D-29: `allow_personal_filters`
    and `allow_unmasked_personal`). A personal dimension with a masking rule may be grouped by
    under every policy."""
    if sensitivity != "personal":
        return
    if use == "filter" and governance.allow_personal_filters:
        return
    if use == "group" and (dimension.masking is not None or governance.allow_unmasked_personal):
        return
    masked = dimension.masking is not None
    instead = [o.ref for o in available.options if o.available and o.sensitivity != "personal"]
    if use == "filter":
        message = (
            f"{ref} is personal data and cannot be used in a filter, masked or not, because "
            "a filter on it would single out people. Remove the filter or filter on a "
            "dimension that is not personal."
        )
    else:
        message = (
            f"{ref} is personal data and has no masked form, so it cannot be returned. "
            "Choose a dimension that is not personal."
        )
    if governance.mode != "strict":
        rule = "allow_personal_filters" if use == "filter" else "allow_unmasked_personal"
        message += (
            f" The governance mode of this server is {governance.mode}; the rule {rule} would "
            "allow this, and the server configuration (not the request) sets it."
        )
    raise QueryError(
        ErrorCode.PERSONAL_DATA_REFUSED,
        message,
        {
            "dimension": ref,
            "use": use,
            "masked_form_available": masked,
            "available_dimensions": instead,
        },
    )


def _planned_dimension(
    model: Model, available: AvailableDimensions, ref: str, governance: GovernancePolicy
) -> PlannedDimension:
    """Checks in this order: the dimension exists, it is not refused as personal data, the
    metric can reach it (section 7.7). A personal dimension with a masking rule is masked while
    the policy applies masks; otherwise it is returned raw, which the policy has allowed."""
    entity, dimension = _find(model, available, ref)
    sensitivity = model.effective_sensitivity(entity, dimension)
    _refuse_personal(available, ref, dimension, sensitivity, "group", governance)
    _check_reachable(available, ref, entity)
    masked = sensitivity == "personal" and dimension.masking is not None and governance.apply_masks
    return PlannedDimension(ref, entity.name, dimension.name, dimension, sensitivity, masked)


def _planned_filter(
    model: Model, available: AvailableDimensions, flt: RequestFilter, governance: GovernancePolicy
) -> PlannedFilter:
    entity, dimension = _find(model, available, flt.dimension)
    sensitivity = model.effective_sensitivity(entity, dimension)
    _refuse_personal(available, flt.dimension, dimension, sensitivity, "filter", governance)
    _check_reachable(available, flt.dimension, entity)
    value = _checked_value(flt, dimension)
    return PlannedFilter(flt.dimension, entity.name, flt.op, value, dimension, sensitivity)


def _invalid_filter(flt: RequestFilter, dimension: Dimension, problem: str) -> QueryError:
    allowed = [v.value for v in dimension.values or []]
    return QueryError(
        ErrorCode.INVALID_FILTER,
        f"Filter on {flt.dimension}: {problem}.",
        {
            "dimension": flt.dimension,
            "expected_type": dimension.type,
            "allowed_operators": list(FILTER_OPS),
            "allowed_values": allowed,
        },
    )


def _checked_value(flt: RequestFilter, dimension: Dimension) -> Scalar | tuple[Scalar, ...] | None:
    op, value = flt.op, flt.value
    if op not in FILTER_OPS:
        raise _invalid_filter(
            flt, dimension, f"operator {op!r} is not one of {', '.join(FILTER_OPS)}"
        )
    if op in ("is_null", "is_not_null"):
        if value is not None:
            raise _invalid_filter(flt, dimension, f"operator {op} takes no value")
        return None
    if op in ("in", "not_in"):
        if not isinstance(value, tuple) or not value:
            raise _invalid_filter(flt, dimension, f"operator {op} needs a non-empty list of values")
        values: tuple[Scalar, ...] = value
    else:
        if value is None or isinstance(value, tuple):
            raise _invalid_filter(flt, dimension, f"operator {op} needs one value")
        values = (value,)
    if dimension.type == "boolean" and op in _ORDERING_OPS:
        raise _invalid_filter(flt, dimension, f"operator {op} does not fit a boolean dimension")
    allowed = [v.value for v in dimension.values or []]
    for item in values:
        if not _type_fits(dimension.type, item):
            raise _invalid_filter(
                flt, dimension, f"value {item!r} does not fit a {dimension.type} dimension"
            )
        if allowed and not any(
            isinstance(item, bool) == isinstance(a, bool) and item == a for a in allowed
        ):
            raise _invalid_filter(
                flt, dimension, f"value {item!r} is not one of the dimension's values"
            )
    return values if isinstance(value, tuple) else value


def _type_fits(dimension_type: str, value: object) -> bool:
    if dimension_type == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if dimension_type == "numeric":
        return isinstance(value, (int, float)) and math.isfinite(value)
    if dimension_type == "time":
        if not isinstance(value, str):
            return False
        try:
            datetime.datetime.fromisoformat(value)
        except ValueError:
            return False
        return True
    return isinstance(value, (str, int, float))


def _personal_date_entities(
    model: Model, request: QueryRequest, measures: Sequence[tuple[Entity, Measure]]
) -> tuple[str, ...]:
    """The entities whose personal official date the request's grain returns. The policy let the
    grain through (`_check_official_dates`), so the date is released; it is guarded like a
    selected personal dimension of that entity."""
    if request.grain is None:
        return ()
    found: dict[str, None] = {}
    for entity, measure in measures:
        date = next((d for d in entity.dimensions if d.name == measure.time_dimension), None)
        if date is not None and model.effective_sensitivity(entity, date) == "personal":
            found[entity.name] = None
    return tuple(found)


def _check_guardable_dates(
    model: Model,
    guard: Sequence[GuardedEntity],
    date_entities: Sequence[str],
) -> None:
    """The records of an entity are counted by its primary key. When the key itself holds a
    personal column (the snapshot date of a balance table), the count would read it, so the
    minimum group size cannot be applied to a grain on that date: PERSONAL_DATA_REFUSED."""
    personal = personal_data.personal_columns(model)
    for guarded in guard:
        if guarded.entity not in date_entities:
            continue
        table = model.entities[guarded.entity].source.table.lower()
        if any((table, key.lower()) in personal for key in guarded.primary_key):
            raise QueryError(
                ErrorCode.PERSONAL_DATA_REFUSED,
                f"The minimum group size cannot be applied to a grain on the personal date of "
                f"{guarded.label}, because the records are counted by a key that holds personal "
                "data. Ask without a grain, or choose another metric.",
                {
                    "dimension": guarded.entity,
                    "use": "time",
                    "masked_form_available": False,
                    "available_dimensions": [],
                },
            )


def _guard(
    model: Model,
    dimensions: Sequence[PlannedDimension],
    governance: GovernancePolicy,
    date_entities: Sequence[str] = (),
) -> tuple[GuardedEntity, ...]:
    """The entities whose records are counted: one for each entity holding a selected personal
    dimension (masked or raw), in the order the dimensions were asked for, then the entities
    whose personal official date a grain returns. None when the policy has no minimum group
    size."""
    if governance.min_group_size == 0:
        return ()
    found: dict[str, GuardedEntity] = {}
    names = [d.entity for d in dimensions if d.personal] + list(date_entities)
    for name in names:
        if name not in found:
            entity = model.entities[name]
            found[name] = GuardedEntity(
                entity.name,
                entity.label,
                tuple(entity.primary_key),
                personal_data.RECORDS_PREFIX + entity.name,
            )
    return tuple(found.values())


# ---- one simple input -----------------------------------------------------------------


def _plan_input(
    model: Model,
    leaf: Metric,
    entity: Entity,
    measure: Measure,
    dimensions: Sequence[PlannedDimension],
    filters: Sequence[PlannedFilter],
    guard: Sequence[GuardedEntity] = (),
) -> InputPlan:
    paths: dict[str, JoinPath] = {}

    def reach(target: str) -> None:
        if target == entity.name or target in paths:
            return
        try:
            paths[target] = resolve_path(model, entity.name, target, leaf.join_paths)
        except QueryError as error:
            raise _in_metric(leaf.name, error) from None

    time_found = next((d for d in entity.dimensions if d.name == measure.time_dimension), None)
    if time_found is None:
        raise _unresolved(
            leaf.name, f"time dimension {entity.name}.{measure.time_dimension} does not exist"
        )
    measure_rule = None
    if measure.rule is not None:
        measure_rule = _planned_rule(model, leaf, f"{entity.name}.{measure.rule}")

    definition_filters: list[PlannedFilter | PlannedRule] = []
    for item in leaf.filters or []:
        if isinstance(item, RuleRef):
            rule = _planned_rule(model, leaf, item.rule)
            reach(rule.entity)
            definition_filters.append(rule)
        else:
            planned = _definition_filter(model, leaf, item)
            reach(planned.entity)
            definition_filters.append(planned)
    for dimension in dimensions:
        reach(dimension.entity)
    for guarded in guard:
        reach(guarded.entity)  # a record count needs the entity on this input's path too
    for flt in filters:
        reach(flt.entity)
    return InputPlan(
        metric=leaf.name,
        measure_entity=entity.name,
        measure=measure,
        time_dimension=time_found,
        measure_rule=measure_rule,
        definition_filters=tuple(definition_filters),
        paths=tuple(EntityPath(name, path) for name, path in paths.items()),
    )


def _planned_rule(model: Model, leaf: Metric, ref: str) -> PlannedRule:
    found = model.rule(ref)
    if found is None:
        raise _unresolved(leaf.name, f"rule {ref} does not exist")
    entity, rule = found
    return PlannedRule(ref, entity.name, rule.name, rule.expr, rule.description)


def _definition_filter(model: Model, leaf: Metric, item: DefinitionFilter) -> PlannedFilter:
    found = model.dimension(item.dimension)
    if found is None:
        raise _unresolved(leaf.name, f"filter dimension {item.dimension} does not exist")
    entity, dimension = found
    value = tuple(item.value) if isinstance(item.value, list) else item.value
    return PlannedFilter(
        item.dimension,
        entity.name,
        item.op,
        value,
        dimension,
        model.effective_sensitivity(entity, dimension),
    )


# ---- result columns (section 7.2) -----------------------------------------------------


def _columns(
    metric: Metric,
    closure: list[Metric],
    grain: str | None,
    dimensions: Sequence[PlannedDimension],
    guard: Sequence[GuardedEntity],
) -> tuple[ResultColumn, ...]:
    by_name = {m.name: m for m in closure}
    columns: list[ResultColumn] = []
    if grain is not None:
        columns.append(ResultColumn(name="metric_time", role="time"))
    columns.extend(ResultColumn(name=d.column, role="dimension") for d in dimensions)
    columns.extend(
        ResultColumn(name=name, role="input", unit=_unit(by_name[name]))
        for name in _direct_inputs(metric)
    )
    columns.append(ResultColumn(name=metric.name, role="metric", unit=_unit(metric)))
    if guard:
        columns.append(ResultColumn(name=personal_data.GROUPS_LEFT_OUT, role="governance"))
    names = [c.name for c in columns]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise personal_data.GovernanceError(
            f"internal error: result columns would share the name {', '.join(repeated)}; "
            "entity, dimension, and metric names must not combine to the same column name"
        )
    return tuple(columns)
