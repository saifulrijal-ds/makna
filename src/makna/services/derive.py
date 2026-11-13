"""Facts the service layer computes for the index and for `get_definition` (ARCHITECTURE 5.4).

The indexer may not import the compiler, so it takes a hook (`makna.index.indexer.Derive`) and
this module is that hook. For each approved or deprecated metric it stores the available
dimensions (section 7.4) as the planner computes them under the configured governance policy
(D-29): the dimension, its path label, its effective sensitivity (the catalog-derived level
included), its masking rule, whether it can be selected, and the reason when it cannot. It also
finds `R05`: a definition that reads a column the catalog marks personal where personal data is
not allowed, which fails the compiler's final check.

`dimension_options` is the one function that turns the planner's answer into the stored form.
The index calls it at build time and the service calls it when the stored facts cannot be used,
so a stored and a computed answer have the same shape and the same source.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal

from makna.compiler.compile import compile_request
from makna.compiler.governance import GovernanceError
from makna.compiler.plan import AvailableDimensions, DimensionOption, available_dimensions
from makna.compiler.snapshot import Model, ReferenceIssue
from makna.index.indexer import Derive, DerivedFacts, IndexContext, IndexIssue
from makna.model.definitions import Entity, Metric
from makna.model.governance import GovernancePolicy
from makna.model.request import QueryError, QueryRequest

logger = logging.getLogger(__name__)

STORED_STATUSES = ("approved", "deprecated")
DIALECT: Literal["postgres", "duckdb"] = "postgres"


def via_label(option: DimensionOption) -> str | None:
    """How the dimension is reached, from the last relationship of its path ("servicing branch").
    None for a dimension of the measure's own entity."""
    for path in option.paths:
        if path.steps:
            return path.steps[-1].partition(".")[2].replace("_", " ")
    return None


def option_to_stored(option: DimensionOption, entity: Entity | None) -> dict[str, Any]:
    dimension = None
    if entity is not None:
        dimension = next((d for d in entity.dimensions if d.name == option.name), None)
    masking: dict[str, Any] | None = None
    if dimension is not None and dimension.masking is not None:
        masking = dimension.masking.model_dump(mode="json")
    stored: dict[str, Any] = {
        "ref": option.ref,
        "entity": option.entity,
        "name": option.name,
        "label": dimension.label if dimension is not None else None,
        "description": option.description,
        "type": option.type,
        "sensitivity": option.sensitivity,
        "masking": masking,
        "available": option.available,
        "reason": option.reason,
        "via": via_label(option),
    }
    return stored


def dimension_options(model: Model, metric: Metric, policy: GovernancePolicy) -> dict[str, Any]:
    """`{"dimensions": [...], "excluded": [...]}` for `metric` under `policy`, JSON-ready.

    Raises `QueryError` (`BROKEN_REFERENCE`) when the metric's inputs do not resolve.
    """
    found: AvailableDimensions = available_dimensions(model, metric, governance=policy)
    return {
        "dimensions": [option_to_stored(o, model.entities.get(o.entity)) for o in found.options],
        "excluded": [
            {"entity": e.entity, "kind": e.kind, "reason": e.reason} for e in found.excluded
        ],
    }


def personal_column_issue(
    table: str, column: str, derived: Mapping[tuple[str, str], str]
) -> tuple[str, str]:
    """`(part, message)` of `R05` for a personal column. Names only, never a value."""
    who = (
        "the catalog marks"
        if derived.get((table.lower(), column.lower())) == "personal"
        else "a definition marks"
    )
    return (
        f"column:{table}.{column}",
        f"{who} column {column} of table {table} as personal, and this metric reads it where "
        "personal data is not allowed (a metric filter, a rule, a measure filter, or a join key)",
    )


def _model_of(context: IndexContext) -> Model:
    entities: list[Entity] = []
    metrics: list[Metric] = []
    # In id order, as the snapshot reads them, so a stored answer lists dimensions in the order a
    # computed one does.
    for obj in sorted(context.objects, key=lambda o: o.id):
        official = obj.official
        if official is None or official.status not in STORED_STATUSES:
            continue
        if isinstance(official, Entity):
            entities.append(official)
        elif isinstance(official, Metric):
            metrics.append(official)
    issues = {
        name: [ReferenceIssue(code, message) for code, message in found]
        for name, found in context.entity_issues.items()
    }
    return Model.build(entities, metrics, issues, context.derived_sensitivity)


def make_derive(policy: GovernancePolicy) -> Derive:
    """The `derive` hook for `makna.index.indexer.build`, bound to the running policy."""

    def derive(context: IndexContext) -> DerivedFacts:
        model = _model_of(context)
        facts: dict[str, dict[str, Any]] = {}
        findings: list[IndexIssue] = []
        for metric in model.metrics.values():
            object_id = f"metric:{metric.name}"
            try:
                facts[object_id] = dimension_options(model, metric, policy)
            except QueryError:
                pass  # broken inputs: the compiler says so at request time
            if metric.status != "approved":
                continue
            try:
                compile_request(model, QueryRequest(metric=metric.name), DIALECT, governance=policy)
            except QueryError:
                continue  # refused for another reason, with a code of its own
            except GovernanceError as error:
                if error.table is None or error.column is None:
                    logger.warning(
                        "indexing: the compiler's final check failed for metric %s (%s)",
                        metric.name,
                        type(error).__name__,
                    )
                    continue
                part, message = personal_column_issue(
                    error.table, error.column, context.derived_sensitivity
                )
                findings.append(IndexIssue(object_id, part, "R05", message))
        return DerivedFacts(facts, findings, {"governance_fingerprint": policy.fingerprint()})

    return derive
