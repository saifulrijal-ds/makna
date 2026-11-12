"""The model snapshot: the entities and metrics the compiler plans against.

A snapshot is plain data. It is built by the caller (the service layer from the index, or the V19
check from a definitions directory) and never changes, so planning is a pure function of a
snapshot and a request. Which definitions belong in it is the caller's decision: the service
layer passes approved and deprecated objects only; V19 also passes proposals.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from makna.model.definitions import Dimension, Entity, Measure, Metric, Relationship, Rule
from makna.model.sensitivity import rank, stricter


@dataclass(frozen=True)
class ReferenceIssue:
    """An open reference issue (R01 to R04) found by comparing a definition with the catalog."""

    code: str
    message: str


def _frozen_mapping[T](items: Mapping[str, T]) -> Mapping[str, T]:
    return MappingProxyType(dict(items))


@dataclass(frozen=True)
class Model:
    """Entities and metrics by name, plus the open reference issues by entity name.

    `derived_sensitivity` maps a dimension, written `entity.dimension`, to the level derived from
    the catalog tags on the columns its expression reads (section 7.6). The caller computes it
    (the indexer stores it); the compiler never reads a catalog.
    """

    entities: Mapping[str, Entity]
    metrics: Mapping[str, Metric]
    reference_issues: Mapping[str, tuple[ReferenceIssue, ...]] = field(default_factory=dict)
    derived_sensitivity: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        entities: Iterable[Entity],
        metrics: Iterable[Metric],
        reference_issues: Mapping[str, Iterable[ReferenceIssue]] | None = None,
        derived_sensitivity: Mapping[str, str] | None = None,
    ) -> Model:
        """Index the objects by name. When a name appears twice, the first one is kept.

        Raises `ValueError` for a derived level that is not a sensitivity level, because a typo
        must not quietly loosen a dimension.
        """
        by_entity: dict[str, Entity] = {}
        for entity in entities:
            by_entity.setdefault(entity.name, entity)
        by_metric: dict[str, Metric] = {}
        for metric in metrics:
            by_metric.setdefault(metric.name, metric)
        issues = {name: tuple(found) for name, found in (reference_issues or {}).items()}
        derived = dict(derived_sensitivity or {})
        for level in derived.values():
            rank(level)
        return cls(
            _frozen_mapping(by_entity),
            _frozen_mapping(by_metric),
            _frozen_mapping({name: found for name, found in issues.items() if found}),
            _frozen_mapping(derived),
        )

    def effective_sensitivity(self, entity: Entity, dimension: Dimension) -> str:
        """The stricter of the dimension's level (the entity's, if it sets none) and the level
        derived from catalog tags (section 7.6)."""
        declared = dimension.sensitivity or entity.sensitivity
        derived = self.derived_sensitivity.get(f"{entity.name}.{dimension.name}")
        return declared if derived is None else stricter(declared, derived)

    # ---- lookups: each returns None when the name does not resolve -------------------

    def dimension(self, ref: str) -> tuple[Entity, Dimension] | None:
        entity_name, _, name = ref.partition(".")
        entity = self.entities.get(entity_name)
        if entity is None:
            return None
        found = next((d for d in entity.dimensions if d.name == name), None)
        return (entity, found) if found is not None else None

    def measure(self, ref: str) -> tuple[Entity, Measure] | None:
        entity_name, _, name = ref.partition(".")
        entity = self.entities.get(entity_name)
        if entity is None:
            return None
        found = next((m for m in entity.measures or [] if m.name == name), None)
        return (entity, found) if found is not None else None

    def rule(self, ref: str) -> tuple[Entity, Rule] | None:
        entity_name, _, name = ref.partition(".")
        entity = self.entities.get(entity_name)
        if entity is None:
            return None
        found = next((r for r in entity.rules or [] if r.name == name), None)
        return (entity, found) if found is not None else None

    def relationship(self, step: str) -> tuple[Entity, Relationship] | None:
        """Look up a join step written `entity.relationship`."""
        entity_name, _, name = step.partition(".")
        entity = self.entities.get(entity_name)
        if entity is None:
            return None
        found = next((r for r in entity.relationships or [] if r.name == name), None)
        return (entity, found) if found is not None else None
