"""What the discovery pages need that no search or definition call gives (WP-17, WEB-1): the
domains, the objects of a domain, the recently approved metrics, and the stored freshness of a
metric.

They follow the visibility rules of `get_definition` and `search` (ARCHITECTURE section 11): a
viewer sees approved objects, and a caller who may see drafts (`permissions.can_see_drafts`) also
sees drafts and objects in review. A deprecated object is left out of every list, as the search
leaves it out; its own page still opens (`get_definition` serves it). These are reads for a
page, not tools: they write no `tool_call_log` row.

Freshness is read from the rows `makna freshness refresh` stored (`makna.services.freshness`); the
engine is never called here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, Engine, select

from makna.index.tables import data_freshness, semantic_object
from makna.model.metrics import dependency_closure
from makna.search.hybrid import visible_statuses
from makna.services import objects, permissions
from makna.services.caller import Caller
from makna.services.freshness import measure_id
from makna.services.snapshot import SnapshotCache
from makna.services.views import cut

LISTED_KINDS = ("metric", "entity", "glossary_term")
"""The kinds a domain page lists, in the order it shows them."""
RECENT_LIMIT = 5


@dataclass(frozen=True)
class Listed:
    """One object in a list: enough for a link, a badge, and a line of meaning."""

    id: str
    kind: str
    name: str
    label: str
    description: str
    status: str | None
    """None when the object is approved; `draft` or `in_review` for a caller who sees drafts."""


@dataclass(frozen=True)
class DomainSummary:
    name: str
    label: str
    description: str
    counts: dict[str, int]
    """Visible objects of the domain by kind (only the kinds a domain page lists)."""


@dataclass(frozen=True)
class DomainListing:
    name: str
    label: str
    description: str
    groups: dict[str, list[Listed]]
    """By kind, in `LISTED_KINDS` order; a kind with no visible object is not a key."""


@dataclass(frozen=True)
class Freshness:
    """How new a metric's data is. `latest` is None when there is nothing to say."""

    latest: date | None = None
    checked_at: datetime | None = None
    """When the oldest of the rows it rests on was checked."""
    measures: int = 0
    """How many measures the metric reads; with several, `latest` is the earliest of theirs."""


def _statuses(caller: Caller) -> list[str]:
    """The statuses of the objects this caller may see in a list (none deprecated)."""
    return visible_statuses("curator" if permissions.can_see_drafts(caller) else "viewer", False)


def _listed(row: Any) -> Listed:
    """`row` is a result row with the columns of `_COLUMNS`."""
    content = row.official or row.proposal or {}
    return Listed(
        id=row.id,
        kind=row.kind,
        name=row.name,
        label=row.label or row.name,
        description=cut(content.get("description")),
        status=None if row.status == "approved" else row.status,
    )


_COLUMNS = (
    semantic_object.c.id,
    semantic_object.c.kind,
    semantic_object.c.name,
    semantic_object.c.label,
    semantic_object.c.status,
    semantic_object.c.official,
    semantic_object.c.proposal,
)


class Discovery:
    """The reads of the discovery pages, on the application database. One per `Services`."""

    def __init__(self, engine: Engine, cache: SnapshotCache) -> None:
        self._engine = engine
        self._cache = cache

    def _objects(
        self, caller: Caller, *where: ColumnElement[bool], limit: int | None = None
    ) -> list[Listed]:
        permissions.require(caller, permissions.Permission.READ_APPROVED)
        query = (
            select(*_COLUMNS)
            .where(semantic_object.c.status.in_(_statuses(caller)), *where)
            .order_by(semantic_object.c.label, semantic_object.c.id)
        )
        if limit is not None:
            query = query.limit(limit)
        with self._engine.connect() as connection:
            return [_listed(row) for row in connection.execute(query)]

    def domains(self, caller: Caller) -> list[DomainSummary]:
        """The domains the caller may see, with how many objects of each kind they hold."""
        found = self._objects(caller, semantic_object.c.kind == "domain")
        members = self._objects_by_domain(caller)
        return [
            DomainSummary(
                name=item.name,
                label=item.label,
                description=item.description,
                counts={
                    kind: len(members.get((item.name, kind), []))
                    for kind in LISTED_KINDS
                    if members.get((item.name, kind))
                },
            )
            for item in found
        ]

    def _objects_by_domain(self, caller: Caller) -> dict[tuple[str, str], list[Listed]]:
        permissions.require(caller, permissions.Permission.READ_APPROVED)
        query = (
            select(*_COLUMNS, semantic_object.c.domain)
            .where(
                semantic_object.c.status.in_(_statuses(caller)),
                semantic_object.c.kind.in_(LISTED_KINDS),
            )
            .order_by(semantic_object.c.label, semantic_object.c.id)
        )
        grouped: dict[tuple[str, str], list[Listed]] = {}
        with self._engine.connect() as connection:
            for row in connection.execute(query):
                if row.domain is not None:
                    grouped.setdefault((row.domain, row.kind), []).append(_listed(row))
        return grouped

    def domain(self, caller: Caller, name: str) -> DomainListing | None:
        """The objects of one domain, grouped by kind. None when the domain does not exist or the
        caller may not see it (the same answer: a draft domain is not revealed)."""
        found = self._objects(
            caller, semantic_object.c.kind == "domain", semantic_object.c.name == name
        )
        if not found:
            return None
        members = self._objects_by_domain(caller)
        groups = {kind: members[name, kind] for kind in LISTED_KINDS if members.get((name, kind))}
        head = found[0]
        return DomainListing(head.name, head.label, head.description, groups)

    def recently_approved(self, caller: Caller, limit: int = RECENT_LIMIT) -> list[Listed]:
        """The approved metrics the index changed last. Without the audit log (WP-18) the time of
        the last index write is the closest thing to "approved recently"."""
        permissions.require(caller, permissions.Permission.READ_APPROVED)
        query = (
            select(*_COLUMNS)
            .where(semantic_object.c.kind == "metric", semantic_object.c.status == "approved")
            .order_by(semantic_object.c.updated_at.desc(), semantic_object.c.id)
            .limit(limit)
        )
        with self._engine.connect() as connection:
            return [_listed(row) for row in connection.execute(query)]

    def freshness(self, caller: Caller, metric_name: str) -> Freshness:
        """The freshness of an approved metric from the stored rows of its measures.

        A metric built from other metrics reads every measure underneath it. `latest` is the
        earliest of the measures' dates, the honest answer for a number that mixes them; it is
        None, "not available", when the metric is not approved, reads no measure, or one of its
        measures has no stored date yet. `checked_at` is the oldest check among them. The date is
        the latest value found in the data: for a schedule column such as the due date it can lie
        in the future. A metric the caller may not see has no freshness, as for `get_definition`.
        """
        permissions.require(caller, permissions.Permission.READ_APPROVED)
        with self._engine.connect() as connection:
            row = objects.fetch_object(connection, f"metric:{metric_name}")
        if row is None or not row.visible_to(permissions.can_see_drafts(caller)):
            return Freshness()
        snapshot, _ = self._cache.current()
        root = snapshot.model.metrics.get(metric_name)
        if root is None:
            return Freshness()
        ids = sorted(
            {
                measure_id(*member.measure.split(".", 1))
                for member in dependency_closure(snapshot.model.metrics, root, strict=False)
                if member.measure
            }
        )
        if not ids:
            return Freshness()
        with self._engine.connect() as connection:
            rows = list(
                connection.execute(
                    select(
                        data_freshness.c.object_id,
                        data_freshness.c.latest_value,
                        data_freshness.c.checked_at,
                    ).where(data_freshness.c.object_id.in_(ids))
                )
            )
        return combine(ids, rows)


def combine(ids: Sequence[str], rows: Sequence[Any]) -> Freshness:
    """The freshness of a metric from the stored rows (`object_id`, `latest_value`, `checked_at`)
    of its measures `ids`."""
    stored = {row.object_id: row for row in rows}
    if not ids or any(i not in stored or stored[i].latest_value is None for i in ids):
        return Freshness(measures=len(ids))
    return Freshness(
        latest=min(stored[i].latest_value for i in ids),
        checked_at=min(stored[i].checked_at for i in ids),
        measures=len(ids),
    )
