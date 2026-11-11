"""The sync job: a full sync of one catalog source into the catalog tables (ARCHITECTURE 6.4).

The job reads everything the connector offers, compares it with the stored copy, and writes
only the difference. It also computes `derived_sensitivity` of each column from its tags
(section 7.6) and, when definitions are given, runs the reference check (section 5.3). A
`sync_run` row records what changed. With `record_unchanged=False`, a run that changes nothing
writes nothing at all (the development stack's `init` uses this so that a second run is a no-op).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, delete, insert, select, update

from makna.catalog import references
from makna.catalog.base import CatalogConnector, ConnectorError
from makna.index.tables import (
    catalog_column,
    catalog_dataset,
    catalog_glossary_term,
    catalog_source,
    sync_run,
)
from makna.model.catalog import CatalogColumn, CatalogDataset, CatalogGlossaryTerm
from makna.model.definitions import Entity, GlossaryTerm
from makna.model.sensitivity import derive_from_tags


@dataclass
class Counts:
    added: int = 0
    changed: int = 0
    removed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"added": self.added, "changed": self.changed, "removed": self.removed}


@dataclass
class SyncSummary:
    datasets: Counts = field(default_factory=Counts)
    datasets_unchanged: int = 0
    columns: Counts = field(default_factory=Counts)
    glossary_terms: Counts = field(default_factory=Counts)
    issues_opened: int = 0
    issues_closed: int = 0
    issues_reworded: int = 0

    @property
    def total_changes(self) -> int:
        counts = (self.datasets, self.columns, self.glossary_terms)
        return (
            sum(c.added + c.changed + c.removed for c in counts)
            + self.issues_opened
            + self.issues_closed
            + self.issues_reworded
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "datasets": {**self.datasets.as_dict(), "unchanged": self.datasets_unchanged},
            "columns": self.columns.as_dict(),
            "glossary_terms": self.glossary_terms.as_dict(),
            "reference_issues": {
                "opened": self.issues_opened,
                "closed": self.issues_closed,
                "reworded": self.issues_reworded,
            },
            "total_changes": self.total_changes,
        }


@dataclass(frozen=True)
class SyncResult:
    summary: SyncSummary
    run_id: int | None
    """Id of the `sync_run` row, or None when nothing changed and no run was recorded."""
    warnings: tuple[str, ...] = ()
    """Things the caller should show: definition files that could not be read."""


def _column_values(
    column: CatalogColumn, position: int, tag_map: Mapping[str, str]
) -> dict[str, Any]:
    return {
        "name": column.name,
        "data_type": column.data_type,
        "native_type": column.native_type,
        "description": column.description,
        "tags": list(column.tags),
        "nullable": column.nullable,
        "is_primary_key": column.is_primary_key,
        "ordinal": column.ordinal if column.ordinal is not None else position,
        "derived_sensitivity": derive_from_tags(column.tags, tag_map),
    }


def _dataset_hash(dataset: CatalogDataset, columns: list[dict[str, Any]]) -> str:
    content = {
        "catalog_id": dataset.catalog_id,
        "name": dataset.name,
        "description": dataset.description,
        "owners": [o.model_dump(mode="json") for o in dataset.owners],
        "tags": list(dataset.tags),
        "columns": columns,
    }
    text = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sync_columns(
    connection: Connection, dataset_id: int, wanted: list[dict[str, Any]], summary: SyncSummary
) -> None:
    stored = {
        row.name: row
        for row in connection.execute(
            select(catalog_column).where(catalog_column.c.dataset_id == dataset_id)
        )
    }
    names = {values["name"] for values in wanted}
    for values in wanted:
        row = stored.get(values["name"])
        if row is None:
            connection.execute(insert(catalog_column).values(dataset_id=dataset_id, **values))
            summary.columns.added += 1
        elif any(getattr(row, key) != value for key, value in values.items()):
            connection.execute(
                update(catalog_column).where(catalog_column.c.id == row.id).values(**values)
            )
            summary.columns.changed += 1
    for name, row in stored.items():
        if name not in names:
            connection.execute(delete(catalog_column).where(catalog_column.c.id == row.id))
            summary.columns.removed += 1


def _sync_datasets(
    connection: Connection,
    source_id: int,
    datasets: list[CatalogDataset],
    tag_map: Mapping[str, str],
    now: datetime,
    summary: SyncSummary,
) -> None:
    stored = {
        row.engine_fqn: row
        for row in connection.execute(
            select(catalog_dataset).where(catalog_dataset.c.source_id == source_id)
        )
    }
    for dataset in datasets:
        columns = [_column_values(c, i, tag_map) for i, c in enumerate(dataset.columns)]
        digest = _dataset_hash(dataset, columns)
        values = {
            "catalog_id": dataset.catalog_id,
            "name": dataset.name,
            "description": dataset.description,
            "owners": [o.model_dump(mode="json") for o in dataset.owners],
            "tags": list(dataset.tags),
            "content_hash": digest,
        }
        row = stored.get(dataset.engine_fqn)
        if row is None:
            dataset_id = connection.execute(
                insert(catalog_dataset)
                .values(source_id=source_id, engine_fqn=dataset.engine_fqn, synced_at=now, **values)
                .returning(catalog_dataset.c.id)
            ).scalar_one()
            summary.datasets.added += 1
        elif row.removed_at is not None:
            connection.execute(
                update(catalog_dataset)
                .where(catalog_dataset.c.id == row.id)
                .values(synced_at=now, removed_at=None, **values)
            )
            dataset_id = row.id
            summary.datasets.added += 1
        elif row.content_hash != digest:
            connection.execute(
                update(catalog_dataset)
                .where(catalog_dataset.c.id == row.id)
                .values(synced_at=now, **values)
            )
            dataset_id = row.id
            summary.datasets.changed += 1
        else:
            summary.datasets_unchanged += 1
            continue
        _sync_columns(connection, dataset_id, columns, summary)
    wanted = {d.engine_fqn for d in datasets}
    for fqn, row in stored.items():
        if fqn not in wanted and row.removed_at is None:
            connection.execute(
                update(catalog_dataset)
                .where(catalog_dataset.c.id == row.id)
                .values(removed_at=now, synced_at=now)
            )
            gone = connection.execute(
                delete(catalog_column).where(catalog_column.c.dataset_id == row.id)
            )
            summary.columns.removed += gone.rowcount
            summary.datasets.removed += 1


def _term_values(term: CatalogGlossaryTerm) -> dict[str, Any]:
    return {"name": term.name, "definition": term.definition, "synonyms": list(term.synonyms)}


def _sync_terms(
    connection: Connection,
    source_id: int,
    terms: list[CatalogGlossaryTerm],
    summary: SyncSummary,
) -> None:
    stored = {
        row.catalog_id: row
        for row in connection.execute(
            select(catalog_glossary_term).where(catalog_glossary_term.c.source_id == source_id)
        )
    }
    for term in terms:
        values = _term_values(term)
        row = stored.get(term.catalog_id)
        if row is None:
            connection.execute(
                insert(catalog_glossary_term).values(
                    source_id=source_id, catalog_id=term.catalog_id, **values
                )
            )
            summary.glossary_terms.added += 1
        elif any(getattr(row, key) != value for key, value in values.items()):
            connection.execute(
                update(catalog_glossary_term)
                .where(catalog_glossary_term.c.id == row.id)
                .values(**values)
            )
            summary.glossary_terms.changed += 1
    wanted = {t.catalog_id for t in terms}
    for catalog_id, row in stored.items():
        if catalog_id not in wanted:
            connection.execute(
                delete(catalog_glossary_term).where(catalog_glossary_term.c.id == row.id)
            )
            summary.glossary_terms.removed += 1


def _refuse_foreign_tables(
    connection: Connection, source_id: int, source_name: str, datasets: list[CatalogDataset]
) -> None:
    """A table already stored under another source means `source.name` was changed."""
    clash = connection.execute(
        select(catalog_dataset.c.engine_fqn, catalog_source.c.name)
        .join(catalog_source, catalog_source.c.id == catalog_dataset.c.source_id)
        .where(
            catalog_dataset.c.source_id != source_id,
            catalog_dataset.c.engine_fqn.in_([d.engine_fqn for d in datasets]),
        )
        .limit(1)
    ).first()
    if clash is not None:
        raise ConnectorError(
            f"source.name {source_name!r}: table {clash[0]} already belongs to source "
            f"{clash[1]!r}; a source cannot be renamed by changing source.name in the file"
        )


def sync_catalog(
    engine: Engine,
    connector: CatalogConnector,
    *,
    tag_map: Mapping[str, str],
    entities: Iterable[Entity] | None = None,
    glossary_terms: Iterable[GlossaryTerm] | None = None,
    unreadable_objects: Mapping[str, str] | None = None,
    record_unchanged: bool = True,
) -> SyncResult:
    """Sync one source into the catalog tables, in one transaction.

    The connector is read in full before the database is touched, so a connector error leaves
    everything as it was. When `entities` is given, the reference check (R01 to R04) runs
    against the new copy and its counts go into the summary; without it, issues are not touched.
    `unreadable_objects` maps object ids to the definition files that failed to load; their open
    issues are left as they are and the result carries a warning for each.
    """
    connector.check()
    datasets = list(connector.list_datasets())
    terms = list(connector.list_glossary_terms())
    started = datetime.now(UTC)
    unreadable = dict(unreadable_objects or {})
    warnings = tuple(
        f"definition file {path} is not valid; reference issues of {object_id} left unchanged"
        for object_id, path in unreadable.items()
    )
    summary = SyncSummary()
    run_id: int | None = None
    with engine.begin() as connection:
        source_id = connection.execute(
            select(catalog_source.c.id).where(catalog_source.c.name == connector.name)
        ).scalar_one_or_none()
        if source_id is None:
            source_id = connection.execute(
                insert(catalog_source)
                .values(name=connector.name, connector=connector.kind)
                .returning(catalog_source.c.id)
            ).scalar_one()
        _refuse_foreign_tables(connection, source_id, connector.name, datasets)
        _sync_datasets(connection, source_id, datasets, tag_map, started, summary)
        _sync_terms(connection, source_id, terms, summary)
        if entities is not None:
            changes = references.check(
                connection, entities, glossary_terms or [], now=started, keep=unreadable
            )
            summary.issues_opened, summary.issues_closed = changes.opened, changes.closed
            summary.issues_reworded = changes.reworded
        if summary.total_changes or record_unchanged:
            finished = datetime.now(UTC)
            connection.execute(
                update(catalog_source)
                .where(catalog_source.c.id == source_id)
                .values(last_sync_at=finished)
            )
            run_id = connection.execute(
                insert(sync_run)
                .values(
                    source_id=source_id,
                    started_at=started,
                    finished_at=finished,
                    status="ok",
                    summary=summary.as_dict(),
                )
                .returning(sync_run.c.id)
            ).scalar_one()
    return SyncResult(summary, run_id, warnings)
