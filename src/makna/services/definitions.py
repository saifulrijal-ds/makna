"""`get_definition`: one object in the form of ARCHITECTURE section 12.

A metric carries its meaning, formula, inputs, time grains, the dimensions it can be grouped by
(with the path that reaches them, the masked form of a personal one, and whether it can be
filtered), the personal dimensions that cannot be used and why, the rules it uses, its example
requests, caveats, and example questions. Entities, glossary terms, dimensions, rules, domains,
and catalog columns and terms return what section 12 says for them.

Two rules hold throughout:

- **No values of a personal dimension.** The allowed values of a dimension are shown only when
  its effective sensitivity is not `personal`, whether declared or derived from the catalog.
- **The available dimensions are the compiler's.** They come from the facts the indexer stored
  when those are valid for the running governance policy, the definitions, and the catalog, and
  otherwise from the planner itself on the snapshot; either way they are `available_dimensions`
  under the configured policy, the same function `compile_query` plans with.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from sqlalchemy import Connection, select

from makna.compiler.plan import allowed_grains
from makna.compiler.snapshot import Model
from makna.index.tables import catalog_column, catalog_dataset, catalog_glossary_term
from makna.model.definitions import (
    Dimension,
    Entity,
    GlossaryTerm,
    Metric,
    RuleRef,
)
from makna.model.definitions import Domain as DomainDefinition
from makna.model.governance import GovernancePolicy
from makna.model.sensitivity import effective_level, stricter
from makna.services import objects
from makna.services.derive import dimension_options
from makna.services.objects import ObjectRow
from makna.services.snapshot import Snapshot
from makna.services.views import (
    DefinitionView,
    DimensionView,
    ExampleRequestView,
    IssueView,
    RelatedRef,
    RelationshipView,
    RuleView,
    TimeView,
    UnavailableDimension,
)

logger = logging.getLogger(__name__)

BALANCE_NOTE = "Balance metric: a period longer than a month reports its last month end."
PERSONAL_REASON = "personal data"
DRAFT_WARNING = (
    "This metric is not approved, so its dimensions are not computed and it cannot be compiled."
)


def masked_text(masking: dict[str, Any] | None) -> str | None:
    """How a masked personal dimension is shown ("year only")."""
    if masking is None:
        return None
    method = masking.get("method")
    if method == "redact":
        return "hidden"
    if method == "truncate_date":
        return f"{masking.get('grain')} only"
    if method == "bucket":
        return f"bands of {masking.get('width'):g}"
    return None


def _values(dimension: Dimension | None, level: str) -> tuple[list[Any], dict[str, str]]:
    """The allowed values of a dimension, and the notes some of them have. None for a personal
    dimension, whatever made it personal."""
    if dimension is None or level == "personal" or not dimension.values:
        return [], {}
    values = [item.value for item in dimension.values]
    notes = {str(item.value): item.description for item in dimension.values if item.description}
    return values, notes


def dimension_view(
    *,
    ref: str,
    entity: Entity,
    dimension: Dimension | None,
    stored: dict[str, Any],
    level: str,
    policy: GovernancePolicy,
    detail: str,
    via: str | None,
) -> DimensionView:
    """The entry of one available dimension. `stored` is the planner's option in stored form
    (`makna.services.derive.option_to_stored`); `level` is the stricter of its level and the
    live one."""
    full = detail == "full"
    values, notes = _values(dimension, level) if full else ([], {})
    view: dict[str, Any] = {
        "ref": ref,
        "label": stored.get("label"),
        "description": stored.get("description") if full else None,
        "type": stored["type"],
        "values": values,
        "value_notes": notes,
        "via": via,
    }
    if level == "personal":
        masking = stored.get("masking")
        has_mask = masking is not None
        raw = (not policy.apply_masks) if has_mask else policy.allow_unmasked_personal
        parts: list[str] = []
        view["sensitivity"] = "personal"
        if raw:
            parts.append(
                f"returned unmasked: the governance mode of this server ({policy.mode}) allows it"
            )
        else:
            view["masked"] = masked_text(masking)
        if not policy.allow_personal_filters:
            view["filterable"] = False
        if policy.min_group_size > 0:
            parts.append(
                f"groups built from fewer than {policy.min_group_size} "
                f"{entity.label.lower()} records are left out"
            )
        view["note"] = "; ".join(parts) or None
    return DimensionView(**view)


def _synonyms(synonyms: dict[str, list[str]] | None) -> list[str]:
    return [word for lang in sorted(synonyms or {}) for word in (synonyms or {})[lang]]


def _closure(model: Model, metric: Metric) -> list[Metric]:
    found: list[Metric] = []
    stack = [metric]
    while stack:
        current = stack.pop()
        if any(m.name == current.name for m in found):
            continue
        found.append(current)
        names = [current.numerator, current.denominator, *(current.inputs or [])]
        for name in names:
            child = model.metrics.get(name) if name else None
            if child is not None:
                stack.append(child)
    return found


def _unit(metric: Metric) -> str | None:
    return metric.format.unit or ("percent" if metric.format.kind == "percent" else None)


class PolicyHolder(Protocol):
    @property
    def policy(self) -> GovernancePolicy: ...


class DefinitionReader:
    """Builds the `DefinitionView` of an id. One per `Services`."""

    def __init__(self, owner: PolicyHolder, on_stale_policy: Callable[[str, str], None]) -> None:
        self._owner = owner
        self._on_stale_policy = on_stale_policy

    @property
    def _policy(self) -> GovernancePolicy:
        """The running policy, read from the services object that owns it."""
        return self._owner.policy

    # --- entry point ---------------------------------------------------------------------

    def read(
        self,
        connection: Connection,
        snapshot: Snapshot,
        object_id: str,
        *,
        sees_drafts: bool,
        detail: str,
    ) -> DefinitionView | None:
        """The definition, or None when it does not exist or the caller may not see it."""
        kind, _, name = object_id.partition(":")
        if kind in ("dimension", "rule"):
            return self._part(connection, snapshot, kind, name, sees_drafts, detail)
        if kind == "column":
            return self._column(connection, name, sees_drafts)
        if kind == "catalog_term":
            return self._catalog_term(connection, name)
        row = objects.fetch_object(connection, object_id)
        if row is None or not row.visible_to(sees_drafts):
            return None
        served = row.definition
        if isinstance(served, Metric):
            return self._metric(connection, snapshot, row, served, sees_drafts, detail)
        if isinstance(served, Entity):
            return self._entity(snapshot, row, served, detail)
        if isinstance(served, GlossaryTerm):
            return self._term(connection, row, served, sees_drafts)
        assert isinstance(served, DomainDefinition)
        return self._header(row, served)

    def exists(self, connection: Connection, object_id: str, *, sees_drafts: bool) -> bool:
        """True when the object exists and the caller may see it (without building its view)."""
        kind, _, name = object_id.partition(":")
        if kind in ("column", "catalog_term"):
            if kind == "column":
                return self._column(connection, name, sees_drafts) is not None
            return self._catalog_term(connection, name) is not None
        owner = f"entity:{name.partition('.')[0]}" if kind in ("dimension", "rule") else object_id
        row = objects.fetch_object(connection, owner)
        if row is None or not row.visible_to(sees_drafts):
            return False
        if kind not in ("dimension", "rule"):
            return True
        entity = row.definition
        part = name.partition(".")[2]
        if not isinstance(entity, Entity):
            return False
        if kind == "rule":
            return any(r.name == part for r in entity.rules or [])
        return any(d.name == part for d in entity.dimensions)

    # --- shared --------------------------------------------------------------------------

    def _header(self, row: ObjectRow, served: Any, **fields: Any) -> DefinitionView:
        synonyms = _synonyms(served.synonyms)
        return DefinitionView(
            id=row.id,
            kind=row.kind,
            label=served.label,
            status=row.status,
            version=row.version,
            description=served.description,
            domain=row.domain,
            owner=served.owner.team,
            sensitivity=served.sensitivity,
            synonyms=synonyms,
            deprecation=(
                {k: v for k, v in served.deprecation.model_dump().items() if v}
                if served.deprecation is not None
                else {}
            ),
            **fields,
        )

    def _curator_fields(self, row: ObjectRow, sees_drafts: bool) -> dict[str, Any]:
        if sees_drafts and row.has_proposal and row.proposal_status:
            return {"proposal_status": row.proposal_status}
        return {}

    # --- metric --------------------------------------------------------------------------

    def _usable_stored(self, row: ObjectRow, snapshot: Snapshot) -> dict[str, Any] | None:
        """The stored dimensions, if they were computed under the running policy, for the
        definitions and the catalog of the snapshot this call uses. The facts and the basis they
        were computed against are one value of one row, so they cannot come from two builds."""
        derived = row.derived or {}
        basis = derived.get("basis")
        if "dimensions" not in derived or not isinstance(basis, dict):
            return None
        running = self._policy.fingerprint()
        stored = str(basis.get("governance_fingerprint"))
        if stored != running:
            self._on_stale_policy(stored, running)
            return None
        if basis.get("definitions_version") != snapshot.key.definitions_version:
            return None
        if basis.get("catalog_marker") != snapshot.key.catalog_marker:
            return None
        return {"dimensions": derived["dimensions"], "excluded": derived.get("excluded", [])}

    def _metric(
        self,
        connection: Connection,
        snapshot: Snapshot,
        row: ObjectRow,
        metric: Metric,
        sees_drafts: bool,
        detail: str,
    ) -> DefinitionView:
        model = snapshot.model
        in_model = row.official is not None and row.status in objects.PUBLIC_STATUSES
        derived = row.derived or {}
        edge_list = objects.edges(connection, ("input_of", "uses_rule"))
        issues: list[IssueView] = []
        warnings: list[str] = []
        dimensions: list[DimensionView] = []
        unavailable: list[UnavailableDimension] = []
        time: TimeView
        rules: list[RuleView] = []
        if in_model:
            served = model.metrics.get(metric.name, metric)
            members = _closure(model, served)
            options: dict[str, Any] | None = self._usable_stored(row, snapshot)
            if options is None:
                try:
                    options = dimension_options(model, served, self._policy)
                except Exception as error:  # QueryError: inputs do not resolve
                    code = getattr(error, "code", None)
                    if code is None:
                        raise
                    options = None
                    issues.append(
                        IssueView(
                            code=str(code), message=getattr(error, "message", "") or str(code)
                        )
                    )
            if options is not None:
                dimensions, unavailable = self._dimensions(model, options["dimensions"], detail)
                if sees_drafts:
                    warnings.extend(
                        item["reason"]
                        for item in options["excluded"]
                        if item["kind"] in ("ambiguous", "inconsistent")
                    )
            time = self._time(model, served, members)
            seen: set[str] = set()
            for rule_id in objects.rules_of_metric(edge_list, row.id):
                found = model.rule(rule_id.partition(":")[2])
                if found is not None and rule_id not in seen:
                    seen.add(rule_id)
                    entity, rule = found
                    rules.append(
                        RuleView(
                            ref=f"{entity.name}.{rule.name}",
                            label=rule.label,
                            description=rule.description,
                        )
                    )
            issues.extend(self._issues(snapshot, members))
        else:
            time = TimeView(
                grains=list(metric.time_grains or []), default_grain=metric.default_grain
            )
            warnings.append(DRAFT_WARNING)
        return self._header(
            row,
            metric,
            formula=derived.get("formula"),
            type=metric.type,
            inputs=list(derived.get("inputs", [])),
            unit=_unit(metric),
            time=time,
            dimensions=dimensions,
            unavailable_dimensions=unavailable,
            rules=rules,
            example_requests=[
                ExampleRequestView(
                    question=example.question,
                    request=example.request.model_dump(mode="json", exclude_none=True),
                )
                for example in metric.example_requests or []
            ],
            caveats=metric.caveats,
            example_questions=list(metric.example_questions or []),
            issues=issues,
            warnings=warnings,
            **self._curator_fields(row, sees_drafts),
        )

    def _issues(self, snapshot: Snapshot, members: Sequence[Metric]) -> list[IssueView]:
        model = snapshot.model
        found: list[IssueView] = []
        entities: list[str] = []
        for member in members:
            found.extend(
                IssueView(code=i.code, message=i.message)
                for i in snapshot.metric_issues.get(member.name, ())
            )
            if member.measure:
                entities.append(member.measure.partition(".")[0])
            for item in member.filters or []:
                ref = item.rule if isinstance(item, RuleRef) else item.dimension
                entities.append(ref.partition(".")[0])
        for name in dict.fromkeys(entities):
            found.extend(
                IssueView(code=i.code, message=i.message)
                for i in model.reference_issues.get(name, ())
            )
        return found

    def _time(self, model: Model, metric: Metric, members: Sequence[Metric]) -> TimeView:
        refs: list[str] = []
        balance = False
        for member in members:
            if member.type != "simple" or not member.measure:
                continue
            found = model.measure(member.measure)
            if found is None:
                continue
            entity, measure = found
            refs.append(f"{entity.name}.{measure.time_dimension}")
            balance = balance or measure.semi_additive == "last"
        refs = list(dict.fromkeys(refs))
        try:
            grains = list(allowed_grains(model, metric))
        except Exception:  # inputs do not resolve: the issues say so
            grains = list(metric.time_grains or [])
        return TimeView(
            dimension=refs[0] if len(refs) == 1 else None,
            dimensions=refs if len(refs) > 1 else [],
            grains=grains,
            default_grain=metric.default_grain,
            note=BALANCE_NOTE if balance else None,
        )

    def _dimensions(
        self, model: Model, options: Sequence[dict[str, Any]], detail: str
    ) -> tuple[list[DimensionView], list[UnavailableDimension]]:
        shown: list[DimensionView] = []
        hidden: list[UnavailableDimension] = []
        for option in options:
            if not option["available"]:
                hidden.append(UnavailableDimension(ref=option["ref"], reason=PERSONAL_REASON))
                continue
            entity = model.entities.get(option["entity"])
            dimension = (
                next((d for d in entity.dimensions if d.name == option["name"]), None)
                if entity is not None
                else None
            )
            level = option["sensitivity"]
            if entity is not None and dimension is not None:
                level = stricter(
                    level, effective_level(entity, dimension, model.derived_sensitivity)
                )
            if entity is None:
                continue
            shown.append(
                dimension_view(
                    ref=option["ref"],
                    entity=entity,
                    dimension=dimension,
                    stored=option,
                    level=level,
                    policy=self._policy,
                    detail=detail,
                    via=option.get("via"),
                )
            )
        return shown, hidden

    # --- entity --------------------------------------------------------------------------

    def _entity(
        self, snapshot: Snapshot, row: ObjectRow, entity: Entity, detail: str
    ) -> DefinitionView:
        derived = snapshot.model.derived_sensitivity
        shown: list[DimensionView] = []
        hidden: list[UnavailableDimension] = []
        for dimension in entity.dimensions:
            level = effective_level(entity, dimension, derived)
            ref = f"{entity.name}.{dimension.name}"
            if (
                level == "personal"
                and dimension.masking is None
                and not self._policy.allow_unmasked_personal
            ):
                hidden.append(UnavailableDimension(ref=ref, reason=PERSONAL_REASON))
                continue
            stored = {
                "label": dimension.label,
                "description": dimension.description,
                "type": dimension.type,
                "masking": dimension.masking.model_dump(mode="json")
                if dimension.masking is not None
                else None,
            }
            shown.append(
                dimension_view(
                    ref=ref,
                    entity=entity,
                    dimension=dimension,
                    stored=stored,
                    level=level,
                    policy=self._policy,
                    detail=detail,
                    via=None,
                )
            )
        return self._header(
            row,
            entity,
            dimensions=shown,
            unavailable_dimensions=hidden,
            rules=[
                RuleView(ref=f"{entity.name}.{r.name}", label=r.label, description=r.description)
                for r in entity.rules or []
            ],
            relationships=[
                RelationshipView(name=r.name, to=r.to, description=r.description)
                for r in entity.relationships or []
            ],
            issues=[
                IssueView(code=i.code, message=i.message)
                for i in snapshot.model.reference_issues.get(entity.name, ())
            ],
        )

    # --- glossary term, dimension, rule ---------------------------------------------------

    def _term(
        self, connection: Connection, row: ObjectRow, term: GlossaryTerm, sees_drafts: bool
    ) -> DefinitionView:
        related_ids = [
            dst for src, dst, kind in objects.edges(connection, ("explains",)) if src == row.id
        ]
        rows = objects.fetch_objects(connection, related_ids)
        related = [
            RelatedRef(id=found.id, label=found.label, kind=found.kind)
            for object_id in related_ids
            if (found := rows.get(object_id)) is not None and found.visible_to(sees_drafts)
        ]
        return self._header(row, term, related=related, **self._curator_fields(row, sees_drafts))

    def _part(
        self,
        connection: Connection,
        snapshot: Snapshot,
        kind: str,
        name: str,
        sees_drafts: bool,
        detail: str,
    ) -> DefinitionView | None:
        entity_name, _, part = name.partition(".")
        row = objects.fetch_object(connection, f"entity:{entity_name}")
        if row is None or not row.visible_to(sees_drafts):
            return None
        entity = row.definition
        assert isinstance(entity, Entity)
        object_id = f"{kind}:{name}"
        if kind == "rule":
            rule = next((r for r in entity.rules or [] if r.name == part), None)
            if rule is None:
                return None
            return DefinitionView(
                id=object_id,
                kind="rule",
                label=rule.label or rule.name.replace("_", " "),
                status=row.status,
                description=rule.description,
                entity=entity.name,
                domain=entity.domain,
            )
        dimension = next((d for d in entity.dimensions if d.name == part), None)
        if dimension is None:
            return None
        level = effective_level(entity, dimension, snapshot.model.derived_sensitivity)
        values, notes = _values(dimension, level) if detail == "full" else ([], {})
        masking = dimension.masking.model_dump(mode="json") if dimension.masking else None
        personal = level == "personal"
        return DefinitionView(
            id=object_id,
            kind="dimension",
            label=dimension.label or dimension.name.replace("_", " "),
            status=row.status,
            description=dimension.description,
            entity=entity.name,
            domain=entity.domain,
            type=dimension.type,
            sensitivity="personal" if personal else None,
            synonyms=_synonyms(dimension.synonyms),
            values=values,
            value_notes=notes,
            caveats=(
                f"Personal data: results show it as {masked_text(masking)}."
                if personal and masking
                else None
            ),
        )

    # --- catalog -------------------------------------------------------------------------

    def _column(
        self, connection: Connection, name: str, sees_drafts: bool
    ) -> DefinitionView | None:
        fqn, _, column = name.rpartition(".")
        found = connection.execute(
            select(
                catalog_dataset.c.engine_fqn,
                catalog_dataset.c.name.label("dataset"),
                catalog_column.c.name,
                catalog_column.c.data_type,
                catalog_column.c.description,
                catalog_column.c.derived_sensitivity,
            )
            .select_from(catalog_column.join(catalog_dataset))
            .where(
                catalog_dataset.c.engine_fqn == fqn,
                catalog_column.c.name == column,
                catalog_dataset.c.removed_at.is_(None),
            )
        ).first()
        if found is None:
            return None
        if found.derived_sensitivity == "personal" and not sees_drafts:
            return None
        return DefinitionView(
            id=f"column:{found.engine_fqn}.{found.name}",
            kind="column",
            label=found.name.replace("_", " "),
            description=found.description or f"Column {found.name} of {found.dataset}.",
            table=found.engine_fqn,
            data_type=found.data_type,
            sensitivity=found.derived_sensitivity,
        )

    def _catalog_term(self, connection: Connection, name: str) -> DefinitionView | None:
        found = connection.execute(
            select(catalog_glossary_term).where(
                (catalog_glossary_term.c.catalog_id == name)
                | (catalog_glossary_term.c.name == name)
            )
        ).first()
        if found is None:
            return None
        return DefinitionView(
            id=f"catalog_term:{found.catalog_id or found.name}",
            kind="catalog_term",
            label=found.name,
            description=found.definition or found.name,
            synonyms=list(found.synonyms or []),
        )
