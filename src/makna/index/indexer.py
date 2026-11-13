"""The indexer: definitions and catalog metadata into `semantic_object` and `search_document`.

`build` rebuilds the whole semantic index from a list of definition objects and the catalog
tables; `index_object` refreshes one object. Both write only the difference: a document whose
text did not change keeps its row, its embedding, and its `text_hash`, and a run that finds no
difference writes nothing (the development stack proves that on a second `init`).

Derived facts (ARCHITECTURE 5.4): this module computes a metric's inputs and its formula line,
the level each entity dimension has once the catalog's tags are counted, and the edges between
objects (`object_edge`). The available dimensions need the planner, which this package may not
import, so the service layer passes a `derive` hook to `build`: it receives the objects and what
the catalog says, and returns the facts to store (WP-12). Without a hook nothing of that kind is
stored, and the service computes the dimensions from the snapshot when it needs them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Connection, Engine, and_, delete, func, insert, select, update

from makna.index.tables import (
    catalog_column,
    catalog_dataset,
    catalog_glossary_term,
    index_state,
    object_edge,
    reference_issue,
    search_document,
    semantic_object,
    sync_run,
)
from makna.model.definitions import (
    Dimension,
    Domain,
    Entity,
    Filter,
    GlossaryTerm,
    Measure,
    Metric,
    RuleRef,
    dump_definition,
)
from makna.model.objects import DefinitionObject
from makna.model.search import EmbeddingProvider
from makna.model.sensitivity import effective_level

EMBED_BATCH = 64
PROPOSAL_STATUSES = ("draft", "in_review")
FIELD_SEPARATOR = " ; "


class IndexerError(Exception):
    """The index cannot be built; the message says why."""


@dataclass(frozen=True)
class SearchDocument:
    """One row of `search_document` before it has an embedding."""

    object_id: str
    kind: str
    status: str
    domain: str | None
    names: str
    description: str
    extra: str

    @property
    def text_hash(self) -> str:
        payload = json.dumps(
            [self.kind, self.status, self.domain, self.names, self.description, self.extra],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def embedding_text(self) -> str:
        """The names twice, so that synonyms weigh more, then the description (section 8.1)."""
        return f"{self.names} {self.names} {self.description}"


@dataclass
class IndexResult:
    """What a run changed, by table."""

    documents_added: int = 0
    documents_changed: int = 0
    documents_removed: int = 0
    documents_unchanged: int = 0
    documents_embedded: int = 0
    objects_added: int = 0
    objects_changed: int = 0
    objects_removed: int = 0
    objects_unchanged: int = 0
    state_changed: int = 0
    edges_added: int = 0
    edges_removed: int = 0
    issues_opened: int = 0
    issues_closed: int = 0
    reembedded_everything: bool = False
    by_kind: dict[str, int] = field(default_factory=dict)
    """Documents in the index after the run, by kind (only for `build`)."""

    @property
    def total_changes(self) -> int:
        return (
            self.documents_added
            + self.documents_changed
            + self.documents_removed
            + self.objects_added
            + self.objects_changed
            + self.objects_removed
            + self.state_changed
            + self.edges_added
            + self.edges_removed
            + self.issues_opened
            + self.issues_closed
        )


# --- Text helpers -----------------------------------------------------------------------------


def _spaced(name: str) -> str:
    return name.replace("_", " ")


def _synonyms(synonyms: Mapping[str, list[str]] | None) -> list[str]:
    return [word for language in sorted(synonyms or {}) for word in (synonyms or {})[language]]


def _names(name: str, label: str | None, synonyms: Mapping[str, list[str]] | None) -> str:
    parts = [_spaced(name)]
    if label and label != parts[0]:
        parts.append(label)
    parts.extend(_synonyms(synonyms))
    return FIELD_SEPARATOR.join(parts)


def _join(parts: Iterable[str]) -> str:
    return FIELD_SEPARATOR.join(p for p in parts if p)


def _value_text(dimension: Dimension, level: str | None = None) -> str:
    """The allowed values as text. A dimension whose effective level is `personal` has none,
    whether it declares that level or the catalog's tags give it (section 7.6)."""
    if (level or dimension.sensitivity) == "personal":
        return ""
    return _join(
        f"{value.value} {value.description}" if value.description else str(value.value)
        for value in dimension.values or []
    )


# --- Formula line (section 5.4) ---------------------------------------------------------------

_AGGREGATES = {
    "sum": "Sum",
    "count": "Count",
    "count_distinct": "Count of distinct",
    "avg": "Average",
    "min": "Minimum",
    "max": "Maximum",
}


class _Lookup:
    """Labels and measures of the served version of every object, for formula lines."""

    def __init__(
        self,
        objects: Iterable[DefinitionObject],
        derived_sensitivity: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self.derived_sensitivity: Mapping[tuple[str, str], str] = derived_sensitivity or {}
        self.entities: dict[str, Entity] = {}
        self.metrics: dict[str, Metric] = {}
        for obj in objects:
            served = obj.served
            if isinstance(served, Entity):
                self.entities.setdefault(served.name, served)
            elif isinstance(served, Metric):
                self.metrics.setdefault(served.name, served)

    def level(self, entity: Entity, dimension: Dimension) -> str:
        """The dimension's effective sensitivity: its declared level, or stricter by the catalog."""
        return effective_level(entity, dimension, self.derived_sensitivity)

    def metric_label(self, name: str) -> str:
        metric = self.metrics.get(name)
        return metric.label if metric else _spaced(name)

    def measure(self, ref: str) -> tuple[Entity | None, Measure | None]:
        entity_name, _, measure_name = ref.partition(".")
        entity = self.entities.get(entity_name)
        if entity is None:
            return None, None
        return entity, next((m for m in entity.measures or [] if m.name == measure_name), None)


def _filter_text(item: Filter | RuleRef, lookup: _Lookup) -> str:
    """A definition filter in words. The constant of a filter on a personal dimension (declared
    or derived from the catalog) is left out."""
    if isinstance(item, RuleRef):
        return _spaced(item.rule.partition(".")[2] or item.rule)
    dimension = _spaced(item.dimension.partition(".")[2] or item.dimension)
    if item.op in ("is_null", "is_not_null"):
        return f"{dimension} {item.op.replace('_', ' ')}"
    entity_name, _, dimension_name = item.dimension.partition(".")
    entity = lookup.entities.get(entity_name)
    found = (
        next((d for d in entity.dimensions if d.name == dimension_name), None) if entity else None
    )
    if entity is not None and found is not None and lookup.level(entity, found) == "personal":
        return f"{dimension} {item.op}"
    value = ", ".join(map(str, item.value)) if isinstance(item.value, list) else item.value
    return f"{dimension} {item.op} {value}"


def formula_line(metric: Metric, lookup: _Lookup) -> str:
    """A plain-language formula, for example "NPF Outstanding Principal divided by ..."."""
    if metric.type == "ratio":
        assert metric.numerator and metric.denominator
        return (
            f"{lookup.metric_label(metric.numerator)} divided by "
            f"{lookup.metric_label(metric.denominator)}"
        )
    if metric.type == "derived":
        assert metric.expr is not None
        text = metric.expr
        for name in sorted(metric.inputs or [], key=len, reverse=True):
            text = re.sub(rf"\b{re.escape(name)}\b", lookup.metric_label(name), text)
        return text
    assert metric.measure is not None
    entity, measure = lookup.measure(metric.measure)
    entity_name, _, measure_name = metric.measure.partition(".")
    where = entity.label if entity else _spaced(entity_name)
    if measure is None:
        line = f"{_spaced(measure_name)} of {where}"
    else:
        what = _spaced(measure.expr or measure_name) if measure.agg != "count" else where
        line = f"{_AGGREGATES[measure.agg]} of {what}"
        if measure.agg != "count":
            line += f" on {where}"
        if measure.semi_additive == "last":
            line += ", last value of the period"
    conditions = [_filter_text(f, lookup) for f in metric.filters or []]
    if conditions:
        line += ", only where " + " and ".join(conditions)
    return line


def metric_inputs(metric: Metric) -> list[str]:
    """Object ids the metric is computed from: its measure, or the metrics it combines."""
    if metric.type == "simple":
        return [f"measure:{metric.measure}"]
    if metric.type == "ratio":
        return [f"metric:{metric.numerator}", f"metric:{metric.denominator}"]
    return [f"metric:{name}" for name in metric.inputs or []]


# --- Documents --------------------------------------------------------------------------------


def served_status(obj: DefinitionObject) -> str:
    """The state of the version search serves.

    An official file keeps its status. An object that has only a proposal is never served as
    approved, whatever the file says: it is `draft` or `in_review`.
    """
    if obj.official is not None:
        return obj.official.status
    status = obj.served.status
    return status if status in PROPOSAL_STATUSES else "draft"


def object_documents(obj: DefinitionObject, lookup: _Lookup) -> list[SearchDocument]:
    """The documents of one object: itself, and for an entity its dimensions and rules."""
    served, status = obj.served, served_status(obj)
    if isinstance(served, Domain):
        return [
            SearchDocument(
                obj.id,
                "domain",
                status,
                served.name,
                _names(served.name, served.label, served.synonyms),
                served.description,
                "",
            )
        ]
    if isinstance(served, GlossaryTerm):
        return [
            SearchDocument(
                obj.id,
                "glossary_term",
                status,
                served.domain,
                _names(served.name, served.label, served.synonyms),
                served.description,
                "",
            )
        ]
    if isinstance(served, Metric):
        questions = list(served.example_questions or [])
        questions += [r.question for r in served.example_requests or []]
        extra = _join([*questions, formula_line(served, lookup), served.caveats or ""])
        return [
            SearchDocument(
                obj.id,
                "metric",
                status,
                served.domain,
                _names(served.name, served.label, served.synonyms),
                served.description,
                extra,
            )
        ]
    assert isinstance(served, Entity)
    documents = [
        SearchDocument(
            obj.id,
            "entity",
            status,
            served.domain,
            _names(served.name, served.label, served.synonyms),
            served.description,
            "",
        )
    ]
    for dimension in served.dimensions:
        documents.append(
            SearchDocument(
                f"dimension:{served.name}.{dimension.name}",
                "dimension",
                status,
                served.domain,
                _names(dimension.name, dimension.label, dimension.synonyms),
                dimension.description,
                _value_text(dimension, lookup.level(served, dimension)),
            )
        )
    for rule in served.rules or []:
        documents.append(
            SearchDocument(
                f"rule:{served.name}.{rule.name}",
                "rule",
                status,
                served.domain,
                _names(rule.name, rule.label, rule.synonyms),
                rule.description,
                "",
            )
        )
    return documents


def owned_ids(obj: DefinitionObject) -> tuple[list[str], list[str]]:
    """The document ids an object owns: exact ids and, for an entity, the id prefixes."""
    if obj.kind == "entity":
        return [obj.id], [f"dimension:{obj.name}.", f"rule:{obj.name}."]
    return [obj.id], []


def catalog_documents(
    connection: Connection, claimed_terms: frozenset[str] = frozenset()
) -> list[SearchDocument]:
    """One document per catalog column of a live dataset, and per unclaimed catalog term.

    A dataset the sync marked removed is skipped, and so are its columns.
    """
    documents: list[SearchDocument] = []
    rows = connection.execute(
        select(
            catalog_dataset.c.engine_fqn,
            catalog_dataset.c.name.label("dataset"),
            catalog_column.c.name,
            catalog_column.c.data_type,
            catalog_column.c.description,
        )
        .select_from(catalog_column.join(catalog_dataset))
        .where(catalog_dataset.c.removed_at.is_(None))
        .order_by(catalog_dataset.c.engine_fqn, catalog_column.c.ordinal)
    )
    for row in rows:
        documents.append(
            SearchDocument(
                f"column:{row.engine_fqn}.{row.name}",
                "column",
                "approved",
                None,
                _spaced(row.name),
                row.description or "",
                _join([row.dataset, row.data_type]),
            )
        )
    terms = connection.execute(select(catalog_glossary_term).order_by(catalog_glossary_term.c.id))
    for term in terms:
        if term.catalog_id in claimed_terms or term.name in claimed_terms:
            continue
        documents.append(
            SearchDocument(
                f"catalog_term:{term.catalog_id or term.name}",
                "glossary_term",
                "approved",
                None,
                _join([term.name, *term.synonyms]),
                term.definition or "",
                "",
            )
        )
    return documents


# --- semantic_object --------------------------------------------------------------------------


def semantic_row(obj: DefinitionObject, lookup: _Lookup) -> dict[str, Any]:
    served = obj.served
    derived: dict[str, Any] | None = None
    if isinstance(served, Metric):
        derived = {"inputs": metric_inputs(served), "formula": formula_line(served, lookup)}
    elif isinstance(served, Entity):
        derived = {
            "dimension_sensitivity": {d.name: lookup.level(served, d) for d in served.dimensions}
        }
    return {
        "id": obj.id,
        "kind": obj.kind,
        "name": obj.name,
        "domain": served.name if isinstance(served, Domain) else getattr(served, "domain", None),
        "label": served.label,
        "status": served_status(obj),
        "has_proposal": obj.proposal is not None,
        "proposal_status": obj.proposal.status if obj.proposal is not None else None,
        "official": dump_definition(obj.official) if obj.official is not None else None,
        "proposal": dump_definition(obj.proposal) if obj.proposal is not None else None,
        "official_path": obj.official_path,
        "official_commit": None,
        "proposal_commit": None,
        "version": served.version,
        "owner_team": served.owner.team,
        "sensitivity": served.sensitivity,
        "derived": derived,
    }


# --- Edges (section 6.4 `object_edge`) --------------------------------------------------------

EDGE_TYPES = ("input_of", "measured_on", "joins_to", "uses_rule", "explains", "replaced_by")
"""What is stored. `same_entity` of section 6.4 is not stored: two metrics are on the same entity
when their `measured_on` entities meet, which the service reads from the edges."""

Edge = tuple[str, str, str]  # (src_id, dst_id, type)


def _qualified(kind: str, ref: str) -> str:
    return ref if ":" in ref else f"{kind}:{ref}"


def object_edges(obj: DefinitionObject, lookup: _Lookup) -> set[Edge]:
    """The edges the served definition of `obj` states.

    - `input_of`: `(input metric, this metric)`, for a ratio or derived metric.
    - `measured_on`: `(this metric, entity)`, for a simple metric.
    - `uses_rule`: `(this metric, rule:<entity>.<rule>)`, for a simple metric, through its
      measure and its filters. A ratio or derived metric uses its inputs' rules, which the
      reader finds through `input_of`.
    - `explains`: `(glossary term, related object)`.
    - `joins_to`: `(entity, target entity)`, one per relationship.
    - `replaced_by`: `(deprecated object, its replacement)`.
    """
    served = obj.served
    edges: set[Edge] = set()
    if served.deprecation is not None and served.deprecation.replaced_by:
        edges.add((obj.id, _qualified(served.kind, served.deprecation.replaced_by), "replaced_by"))
    if isinstance(served, Metric):
        if served.type == "simple" and served.measure:
            entity_name = served.measure.partition(".")[0]
            edges.add((obj.id, f"entity:{entity_name}", "measured_on"))
            entity, measure = lookup.measure(served.measure)
            if entity is not None and measure is not None and measure.rule:
                edges.add((obj.id, f"rule:{entity.name}.{measure.rule}", "uses_rule"))
            for item in served.filters or []:
                if isinstance(item, RuleRef):
                    edges.add((obj.id, f"rule:{item.rule}", "uses_rule"))
        for input_id in metric_inputs(served):
            if input_id.startswith("metric:"):
                edges.add((input_id, obj.id, "input_of"))
    elif isinstance(served, GlossaryTerm):
        for related in served.related or []:
            edges.add((obj.id, related, "explains"))
    elif isinstance(served, Entity):
        for relationship in served.relationships or []:
            edges.add((obj.id, f"entity:{relationship.to}", "joins_to"))
    return edges


def _edge_scope(owner_ids: Sequence[str] | None) -> ColumnElement[bool]:
    """The edges stated by these objects: an `input_of` edge by the metric it points to (the one
    that lists its inputs), every other type by its source. None is every edge."""
    if owner_ids is None:
        return object_edge.c.src_id.is_not(None)
    owned = list(owner_ids)
    return and_(object_edge.c.type == "input_of", object_edge.c.dst_id.in_(owned)) | and_(
        object_edge.c.type != "input_of", object_edge.c.src_id.in_(owned)
    )


def _sync_edges(
    connection: Connection,
    wanted: set[Edge],
    owner_ids: Sequence[str] | None,
    result: IndexResult,
) -> None:
    existing = {
        (row.src_id, row.dst_id, row.type)
        for row in connection.execute(
            select(object_edge.c.src_id, object_edge.c.dst_id, object_edge.c.type).where(
                _edge_scope(owner_ids)
            )
        )
    }
    for src, dst, kind in sorted(existing - wanted):
        connection.execute(
            delete(object_edge).where(
                object_edge.c.src_id == src,
                object_edge.c.dst_id == dst,
                object_edge.c.type == kind,
            )
        )
    result.edges_removed += len(existing - wanted)
    fresh = sorted(wanted - existing)
    if fresh:
        connection.execute(
            insert(object_edge),
            [{"src_id": src, "dst_id": dst, "type": kind} for src, dst, kind in fresh],
        )
        result.edges_added += len(fresh)


# --- What the catalog says --------------------------------------------------------------------


def catalog_sensitivity(connection: Connection) -> dict[tuple[str, str], str]:
    """`{(engine_fqn, column): level}` in lower case, for the columns of live datasets whose tags
    give a level (`catalog_column.derived_sensitivity`). This is the map `Model` takes."""
    rows = connection.execute(
        select(
            catalog_dataset.c.engine_fqn,
            catalog_column.c.name,
            catalog_column.c.derived_sensitivity,
        )
        .select_from(catalog_column.join(catalog_dataset))
        .where(
            catalog_dataset.c.removed_at.is_(None),
            catalog_column.c.derived_sensitivity.is_not(None),
        )
    )
    return {(fqn.lower(), name.lower()): level for fqn, name, level in rows}


def catalog_marker(connection: Connection) -> str:
    """The id of the latest successful catalog sync (`0` if there is none). It changes whenever
    a sync changed anything, so derived facts can tell that the catalog moved on."""
    value = connection.execute(
        select(func.coalesce(func.max(sync_run.c.id), 0)).where(sync_run.c.status == "ok")
    ).scalar_one()
    return str(value)


# --- Facts from the service layer -------------------------------------------------------------

DERIVED_ISSUE_CODES = ("R05",)
"""Reference issue codes the indexer owns: it opens and closes them, and the catalog sync leaves
them alone (`makna.catalog.references.plan_changes`)."""


@dataclass(frozen=True)
class IndexIssue:
    """A reference issue found at index time (`R05`)."""

    object_id: str
    part: str
    code: str
    message: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.object_id, self.part, self.code)


@dataclass(frozen=True)
class IndexContext:
    """What a `derive` hook gets: the objects being indexed and what the catalog says."""

    objects: Sequence[DefinitionObject]
    derived_sensitivity: Mapping[tuple[str, str], str]
    entity_issues: Mapping[str, tuple[tuple[str, str], ...]]
    """Open reference issues of entities, `entity name -> ((code, message), ...)`."""


@dataclass(frozen=True)
class DerivedFacts:
    """What a `derive` hook returns. `object_facts` are merged into `semantic_object.derived`,
    `issues` become open `R05` rows (all other open `R05` rows are closed), and `state` is written
    to `index_state` (the policy fingerprint and what the facts were computed against)."""

    object_facts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    issues: Sequence[IndexIssue] = ()
    state: Mapping[str, str] = field(default_factory=dict)


Derive = Callable[[IndexContext], DerivedFacts]


def _entity_issues(connection: Connection) -> dict[str, tuple[tuple[str, str], ...]]:
    found: dict[str, list[tuple[str, str]]] = {}
    rows = connection.execute(
        select(reference_issue.c.object_id, reference_issue.c.code, reference_issue.c.message)
        .where(
            reference_issue.c.closed_at.is_(None),
            func.starts_with(reference_issue.c.object_id, "entity:"),
        )
        .order_by(reference_issue.c.id)
    )
    for object_id, code, message in rows:
        found.setdefault(object_id.partition(":")[2], []).append((code, message))
    return {name: tuple(items) for name, items in found.items()}


def _sync_issues(
    connection: Connection, findings: Sequence[IndexIssue], result: IndexResult
) -> None:
    """Make the open rows of the codes the indexer owns equal `findings`."""
    now = datetime.now(UTC)
    found = {f.key: f for f in findings}
    open_keys: set[tuple[str, str, str]] = set()
    for row in connection.execute(
        select(reference_issue)
        .where(
            reference_issue.c.closed_at.is_(None),
            reference_issue.c.code.in_(DERIVED_ISSUE_CODES),
        )
        .order_by(reference_issue.c.id)
    ):
        key = (row.object_id, row.part, row.code)
        current = found.get(key)
        if current is None or key in open_keys:
            connection.execute(
                update(reference_issue).where(reference_issue.c.id == row.id).values(closed_at=now)
            )
            result.issues_closed += 1
            continue
        open_keys.add(key)
        if current.message != row.message:
            connection.execute(
                update(reference_issue)
                .where(reference_issue.c.id == row.id)
                .values(message=current.message)
            )
    fresh = [
        {
            "object_id": f.object_id,
            "part": f.part,
            "code": f.code,
            "message": f.message,
            "opened_at": now,
        }
        for key, f in found.items()
        if key not in open_keys
    ]
    if fresh:
        connection.execute(insert(reference_issue), fresh)
        result.issues_opened += len(fresh)


def definitions_version(objects: Iterable[DefinitionObject]) -> str:
    """A digest of all definition content: it changes when any definition changes."""
    digest = hashlib.sha256()
    for obj in sorted(objects, key=lambda o: o.id):
        digest.update(json.dumps(semantic_payload(obj), sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]


def semantic_payload(obj: DefinitionObject) -> list[Any]:
    return [
        obj.id,
        dump_definition(obj.official) if obj.official is not None else None,
        dump_definition(obj.proposal) if obj.proposal is not None else None,
    ]


# --- Writing ----------------------------------------------------------------------------------


def _claimed_terms(objects: Iterable[DefinitionObject]) -> frozenset[str]:
    return frozenset(
        o.served.catalog_term
        for o in objects
        if isinstance(o.served, GlossaryTerm) and o.served.catalog_term
    )


def _sync_documents(
    connection: Connection,
    provider: EmbeddingProvider,
    wanted: Sequence[SearchDocument],
    scope: ColumnElement[bool],
    *,
    force: bool,
    result: IndexResult,
) -> None:
    """Make the rows in `scope` (a SQL condition on search_document) equal to `wanted`."""
    existing = {
        row.object_id: row
        for row in connection.execute(
            select(search_document.c.id, search_document.c.object_id, search_document.c.text_hash)
            .where(scope)
            .order_by(search_document.c.id)
        )
    }
    wanted_ids = {d.object_id for d in wanted}
    gone = [row.id for oid, row in existing.items() if oid not in wanted_ids]
    todo = [
        d
        for d in wanted
        if force or d.object_id not in existing or existing[d.object_id].text_hash != d.text_hash
    ]
    result.documents_unchanged += len(wanted) - len(todo)
    if gone:
        connection.execute(delete(search_document).where(search_document.c.id.in_(gone)))
        result.documents_removed += len(gone)
    vectors: list[list[float]] = []
    for start in range(0, len(todo), EMBED_BATCH):
        batch = todo[start : start + EMBED_BATCH]
        found = provider.embed_documents([d.embedding_text for d in batch])
        if len(found) != len(batch) or any(len(v) != provider.dimension for v in found):
            raise IndexerError(
                f"embedding provider {provider.model_id} did not return "
                f"{len(batch)} vectors of {provider.dimension} numbers"
            )
        vectors.extend(found)
    result.documents_embedded += len(todo)
    fresh: list[dict[str, Any]] = []
    for document, vector in zip(todo, vectors, strict=True):
        values = {
            "object_id": document.object_id,
            "kind": document.kind,
            "status": document.status,
            "domain": document.domain,
            "names": document.names,
            "description": document.description,
            "extra": document.extra,
            "embedding": vector,
            "text_hash": document.text_hash,
        }
        if document.object_id in existing:
            connection.execute(
                update(search_document)
                .where(search_document.c.id == existing[document.object_id].id)
                .values(**values)
            )
            result.documents_changed += 1
        else:
            fresh.append(values)
    if fresh:
        connection.execute(insert(search_document), fresh)
        result.documents_added += len(fresh)


def _sync_semantic_objects(
    connection: Connection,
    rows: Sequence[dict[str, Any]],
    scope: ColumnElement[bool],
    result: IndexResult,
) -> None:
    existing = {
        row.id: row._asdict()
        for row in connection.execute(
            select(*[c for c in semantic_object.c if c.name != "updated_at"]).where(scope)
        )
    }
    wanted_ids = {row["id"] for row in rows}
    gone = [oid for oid in existing if oid not in wanted_ids]
    if gone:
        connection.execute(delete(semantic_object).where(semantic_object.c.id.in_(gone)))
        result.objects_removed += len(gone)
    fresh: list[dict[str, Any]] = []
    for row in rows:
        before = existing.get(row["id"])
        if before is None:
            fresh.append(row)
        elif before != row:
            connection.execute(
                update(semantic_object)
                .where(semantic_object.c.id == row["id"])
                .values(**row, updated_at=func.now())
            )
            result.objects_changed += 1
        else:
            result.objects_unchanged += 1
    if fresh:
        connection.execute(insert(semantic_object), fresh)
        result.objects_added += len(fresh)


def _set_state(connection: Connection, key: str, value: str, result: IndexResult) -> None:
    current = connection.execute(
        select(index_state.c.value).where(index_state.c.key == key)
    ).scalar_one_or_none()
    if current == value:
        return
    if current is None:
        connection.execute(insert(index_state).values(key=key, value=value))
    else:
        connection.execute(update(index_state).where(index_state.c.key == key).values(value=value))
    result.state_changed += 1


def _model_changed(connection: Connection, provider: EmbeddingProvider) -> bool:
    current = connection.execute(
        select(index_state.c.value).where(index_state.c.key == "embedding_model")
    ).scalar_one_or_none()
    return current is not None and current != provider.model_id


def build(
    engine: Engine,
    provider: EmbeddingProvider,
    objects: Sequence[DefinitionObject],
    *,
    derive: Derive | None = None,
) -> IndexResult:
    """Rebuild the semantic index from `objects` and the catalog tables.

    Everything in the index that is not in the input is removed: an object whose files are gone,
    a column of a removed dataset. If `provider` is not the model that built the index, every
    document is embedded again. The whole run is one transaction.

    `derive` is the service layer's hook (see the module docstring): when given, its facts are
    merged into the metric rows and its `R05` issues are synced; when not, the stored dimensions
    and the `R05` rows are left as they are.
    """
    result = IndexResult()
    with engine.begin() as connection:
        # The marker first: a sync that commits after it is not claimed as covered by the facts.
        marker = catalog_marker(connection)
        sensitivity = catalog_sensitivity(connection)
        lookup = _Lookup(objects, sensitivity)
        facts = (
            derive(IndexContext(objects, sensitivity, _entity_issues(connection)))
            if derive is not None
            else None
        )
        wanted: list[SearchDocument] = []
        for obj in objects:
            wanted.extend(object_documents(obj, lookup))
        rows = [semantic_row(obj, lookup) for obj in objects]
        if facts is not None:
            basis = {
                "governance_fingerprint": facts.state.get("governance_fingerprint", ""),
                "definitions_version": definitions_version(objects),
                "catalog_marker": marker,
            }
            for row in rows:
                extra = facts.object_facts.get(row["id"])
                if extra:
                    # The basis is stored with the facts, so a reader gets both in one row.
                    row["derived"] = {**(row["derived"] or {}), **extra, "basis": basis}
        wanted.extend(catalog_documents(connection, _claimed_terms(objects)))
        force = _model_changed(connection, provider)
        result.reembedded_everything = force
        _sync_documents(
            connection,
            provider,
            wanted,
            search_document.c.id.is_not(None),
            force=force,
            result=result,
        )
        _sync_semantic_objects(connection, rows, semantic_object.c.id.is_not(None), result)
        edges: set[Edge] = set()
        for obj in objects:
            edges |= object_edges(obj, lookup)
        _sync_edges(connection, edges, None, result)
        if facts is not None:
            _sync_issues(connection, facts.issues, result)
        _set_state(connection, "embedding_model", provider.model_id, result)
        _set_state(connection, "definitions_version", definitions_version(objects), result)
        if facts is not None:
            # The facts hold for these definitions and this catalog; a reader that sees another
            # `definitions_version` or catalog marker knows they are stale.
            state_basis = {
                "derived_definitions_version": definitions_version(objects),
                "derived_catalog_marker": marker,
            }
            for key, value in sorted({**facts.state, **state_basis}.items()):
                _set_state(connection, key, value, result)
        counts = connection.execute(
            select(search_document.c.kind, func.count()).group_by(search_document.c.kind)
        )
        result.by_kind = {kind: count for kind, count in counts}
    return result


def _bumped(version: str | None, payload: Any) -> str:
    """The next `definitions_version` after one object changed: a digest of the old one and the
    object's new content, so it differs from every earlier value."""
    digest = hashlib.sha256()
    digest.update((version or "").encode("utf-8"))
    digest.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]


def index_object(
    engine: Engine,
    provider: EmbeddingProvider,
    obj: DefinitionObject,
    others: Iterable[DefinitionObject] = (),
) -> IndexResult:
    """Refresh the rows of one object (for an entity also its dimensions and rules).

    `others` are the other objects of the repository; they only supply labels for the formula
    line. No other object's rows are read or written, except that the edges `obj` states are
    replaced, and that `index_state.definitions_version` moves on when anything changed, so a
    process that caches the model refreshes it (ARCHITECTURE section 4.2).

    The available dimensions stored for `obj` are kept as they are: the planner is not available
    here. The new `definitions_version` marks every stored dimension stale until the next
    `build`, and the service computes them from the snapshot meanwhile.
    """
    exact, prefixes = owned_ids(obj)
    scope: ColumnElement[bool] = search_document.c.object_id.in_(exact)
    for prefix in prefixes:
        scope = scope | func.starts_with(search_document.c.object_id, prefix)
    result = IndexResult()
    with engine.begin() as connection:
        if _model_changed(connection, provider):
            raise IndexerError(
                "the index was built with another embedding model; run `makna index build`"
            )
        lookup = _Lookup([obj, *others], catalog_sensitivity(connection))
        row = semantic_row(obj, lookup)
        kept = connection.execute(
            select(semantic_object.c.derived).where(semantic_object.c.id == obj.id)
        ).scalar_one_or_none()
        if isinstance(kept, dict):
            row["derived"] = {**kept, **(row["derived"] or {})}
        _sync_documents(
            connection, provider, object_documents(obj, lookup), scope, force=False, result=result
        )
        _sync_semantic_objects(connection, [row], semantic_object.c.id == obj.id, result)
        _sync_edges(connection, object_edges(obj, lookup), [obj.id], result)
        if result.total_changes:
            current = connection.execute(
                select(index_state.c.value).where(index_state.c.key == "definitions_version")
            ).scalar_one_or_none()
            _set_state(
                connection, "definitions_version", _bumped(current, semantic_payload(obj)), result
            )
    return result
