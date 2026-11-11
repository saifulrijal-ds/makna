"""The indexer: definitions and catalog metadata into `semantic_object` and `search_document`.

`build` rebuilds the whole semantic index from a list of definition objects and the catalog
tables; `index_object` refreshes one object. Both write only the difference: a document whose
text did not change keeps its row, its embedding, and its `text_hash`, and a run that finds no
difference writes nothing (the development stack proves that on a second `init`).

Derived facts (ARCHITECTURE 5.4): this module computes a metric's inputs and its formula line.
The available dimensions need the planner and are added by the service layer (WP-12).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, Connection, Engine, delete, func, insert, select, update

from makna.index.tables import (
    catalog_column,
    catalog_dataset,
    catalog_glossary_term,
    index_state,
    search_document,
    semantic_object,
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


def _value_text(dimension: Dimension) -> str:
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

    def __init__(self, objects: Iterable[DefinitionObject]) -> None:
        self.entities: dict[str, Entity] = {}
        self.metrics: dict[str, Metric] = {}
        for obj in objects:
            served = obj.served
            if isinstance(served, Entity):
                self.entities.setdefault(served.name, served)
            elif isinstance(served, Metric):
                self.metrics.setdefault(served.name, served)

    def metric_label(self, name: str) -> str:
        metric = self.metrics.get(name)
        return metric.label if metric else _spaced(name)

    def measure(self, ref: str) -> tuple[Entity | None, Measure | None]:
        entity_name, _, measure_name = ref.partition(".")
        entity = self.entities.get(entity_name)
        if entity is None:
            return None, None
        return entity, next((m for m in entity.measures or [] if m.name == measure_name), None)


def _filter_text(item: Filter | RuleRef) -> str:
    if isinstance(item, RuleRef):
        return _spaced(item.rule.partition(".")[2] or item.rule)
    dimension = _spaced(item.dimension.partition(".")[2] or item.dimension)
    if item.op in ("is_null", "is_not_null"):
        return f"{dimension} {item.op.replace('_', ' ')}"
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
    conditions = [_filter_text(f) for f in metric.filters or []]
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
                _value_text(dimension),
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
    include_catalog: bool = True,
) -> IndexResult:
    """Rebuild the semantic index from `objects` and the catalog tables.

    Everything in the index that is not in the input is removed: an object whose files are gone,
    a column of a removed dataset. If `provider` is not the model that built the index, every
    document is embedded again. The whole run is one transaction.
    """
    lookup = _Lookup(objects)
    wanted: list[SearchDocument] = []
    for obj in objects:
        wanted.extend(object_documents(obj, lookup))
    rows = [semantic_row(obj, lookup) for obj in objects]
    result = IndexResult()
    with engine.begin() as connection:
        if include_catalog:
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
        _set_state(connection, "embedding_model", provider.model_id, result)
        _set_state(connection, "definitions_version", definitions_version(objects), result)
        counts = connection.execute(
            select(search_document.c.kind, func.count()).group_by(search_document.c.kind)
        )
        result.by_kind = {kind: count for kind, count in counts}
    return result


def index_object(
    engine: Engine,
    provider: EmbeddingProvider,
    obj: DefinitionObject,
    others: Iterable[DefinitionObject] = (),
) -> IndexResult:
    """Refresh the rows of one object (for an entity also its dimensions and rules).

    `others` are the other objects of the repository; they only supply labels for the formula
    line. No other object's rows are read or written.
    """
    lookup = _Lookup([obj, *others])
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
        _sync_documents(
            connection, provider, object_documents(obj, lookup), scope, force=False, result=result
        )
        _sync_semantic_objects(
            connection, [semantic_row(obj, lookup)], semantic_object.c.id == obj.id, result
        )
    return result
