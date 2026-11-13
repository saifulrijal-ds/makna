"""The approved model snapshot, cached in memory and refreshed when the index moves on.

The snapshot is what the compiler plans against: the official content of approved and deprecated
entities and metrics, the open reference issues, and the levels the catalog derives for columns
(ARCHITECTURE sections 4.2 and 5.4). It is built from the database, never from files.

It is cached per process. Every call reads a small key (the `definitions_version` of the index,
the latest successful catalog sync, and the open reference issues) and compares it with the key
the cached snapshot was built from. When they differ, the snapshot is rebuilt in one
repeatable-read transaction, so its content and its key belong together. A lock lets one thread
rebuild; a thread that finds a rebuild running keeps serving the old snapshot until the new one
is ready.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from sqlalchemy import Connection, Engine, func, select

from makna.compiler.snapshot import Model, ReferenceIssue
from makna.index.indexer import catalog_marker, catalog_sensitivity
from makna.index.tables import (
    catalog_dataset,
    index_state,
    reference_issue,
    semantic_object,
    sync_run,
)
from makna.model.definitions import Entity, Metric, parse_definition

STATE_KEYS = (
    "definitions_version",
    "governance_fingerprint",
    "derived_definitions_version",
    "derived_catalog_marker",
)
SERVED_STATUSES = ("approved", "deprecated")


@dataclass(frozen=True)
class StateKey:
    """What the snapshot depends on. Equal keys mean the same content."""

    definitions_version: str
    catalog_marker: str
    issues: str


@dataclass(frozen=True)
class Snapshot:
    key: StateKey
    model: Model
    metric_issues: Mapping[str, tuple[ReferenceIssue, ...]] = field(default_factory=dict)
    """Open `R05` issues by metric name (the compiler's model holds entity issues only)."""


@dataclass(frozen=True)
class CurrentState:
    """The index state read for this call: the key and the facts-basis values of `index_state`."""

    key: StateKey
    state: Mapping[str, str]
    catalog_synced: bool


def read_state(connection: Connection) -> CurrentState:
    """One cheap read: the index state, the catalog marker, and whether a catalog is synced."""
    state = {
        row.key: row.value
        for row in connection.execute(
            select(index_state.c.key, index_state.c.value).where(index_state.c.key.in_(STATE_KEYS))
        )
    }
    issues = hashlib.sha256()
    for row in connection.execute(
        select(reference_issue.c.id, reference_issue.c.code, reference_issue.c.message)
        .where(reference_issue.c.closed_at.is_(None))
        .order_by(reference_issue.c.id)
    ):
        issues.update(f"{row.id}|{row.code}|{row.message}\n".encode())
    marker = catalog_marker(connection)
    has_run = connection.execute(
        select(func.count()).select_from(sync_run).where(sync_run.c.status == "ok")
    ).scalar_one()
    has_dataset = connection.execute(
        select(func.count())
        .select_from(catalog_dataset)
        .where(catalog_dataset.c.removed_at.is_(None))
    ).scalar_one()
    key = StateKey(state.get("definitions_version", ""), marker, issues.hexdigest()[:16])
    return CurrentState(key, state, bool(has_run) and bool(has_dataset))


def _build(connection: Connection, key: StateKey) -> Snapshot:
    entities: list[Entity] = []
    metrics: list[Metric] = []
    rows = connection.execute(
        select(semantic_object.c.kind, semantic_object.c.official)
        .where(
            semantic_object.c.official.is_not(None),
            semantic_object.c.status.in_(SERVED_STATUSES),
            semantic_object.c.kind.in_(("entity", "metric")),
        )
        .order_by(semantic_object.c.id)
    )
    for kind, official in rows:
        definition = parse_definition(official)
        if kind == "entity" and isinstance(definition, Entity):
            entities.append(definition)
        elif kind == "metric" and isinstance(definition, Metric):
            metrics.append(definition)
    by_entity: dict[str, list[ReferenceIssue]] = {}
    by_metric: dict[str, list[ReferenceIssue]] = {}
    for row in connection.execute(
        select(reference_issue.c.object_id, reference_issue.c.code, reference_issue.c.message)
        .where(reference_issue.c.closed_at.is_(None))
        .order_by(reference_issue.c.id)
    ):
        kind, _, name = row.object_id.partition(":")
        target = by_entity if kind == "entity" else by_metric if kind == "metric" else None
        if target is not None:
            target.setdefault(name, []).append(ReferenceIssue(row.code, row.message))
    model = Model.build(entities, metrics, by_entity, catalog_sensitivity(connection))
    return Snapshot(
        key,
        model,
        MappingProxyType({name: tuple(found) for name, found in by_metric.items()}),
    )


class SnapshotCache:
    """The snapshot of one process, rebuilt when the key it was built from is out of date."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    def current(self) -> tuple[Snapshot, CurrentState]:
        """The snapshot to use for this call, and the index state read for it."""
        with self._engine.connect() as connection:
            state = read_state(connection)
        snapshot = self._snapshot
        if snapshot is not None and snapshot.key == state.key:
            return snapshot, state
        if snapshot is not None:
            if self._lock.acquire(blocking=False):
                try:
                    return self._rebuild(), state
                finally:
                    self._lock.release()
            return snapshot, state  # another thread is rebuilding: serve the old one meanwhile
        with self._lock:
            snapshot = self._snapshot
            if snapshot is not None and snapshot.key == state.key:
                return snapshot, state
            return self._rebuild(), state

    def _rebuild(self) -> Snapshot:
        """Build in one repeatable-read transaction and keep the key read inside it, so the
        content and the key cannot come from two moments. The caller holds the lock."""
        with self._engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            key = read_state(c).key
            built = _build(c, key)
        self._snapshot = built
        return built

    def peek(self) -> Snapshot | None:
        """The cached snapshot, if any, without reading the database (for tests)."""
        return self._snapshot
