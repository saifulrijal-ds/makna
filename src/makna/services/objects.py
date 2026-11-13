"""Reading objects and the graph from the index, and who may see what (ARCHITECTURE 4.1, 11).

Visibility is decided here and nowhere else: approved and deprecated objects are visible to
everyone who may read; drafts and proposals only to callers who may see them. An object the caller
may not see is reported as absent, in the same way as an id that does not exist, so a draft's
existence is not revealed.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, select

from makna.index.tables import object_edge, semantic_object
from makna.model.definitions import AnyDefinition, Entity, parse_definition

PUBLIC_STATUSES = ("approved", "deprecated")
ID_KINDS = (
    "metric",
    "entity",
    "glossary_term",
    "domain",
    "dimension",
    "rule",
    "column",
    "catalog_term",
)
MAX_ID_LENGTH = 200
ID_PATTERN = re.compile(r"^(" + "|".join(ID_KINDS) + r"):([A-Za-z0-9_][A-Za-z0-9_.\-]*)$")


@dataclass(frozen=True)
class ObjectRow:
    id: str
    kind: str
    name: str
    label: str
    status: str
    domain: str | None
    has_proposal: bool
    proposal_status: str | None
    official: dict[str, Any] | None
    proposal: dict[str, Any] | None
    derived: dict[str, Any] | None
    version: int | None

    @property
    def definition(self) -> AnyDefinition:
        """The version that is served: the official file, else the proposal."""
        content = self.official if self.official is not None else self.proposal
        return parse_definition(content)

    def visible_to(self, sees_drafts: bool) -> bool:
        return self.status in PUBLIC_STATUSES or sees_drafts


_COLUMNS = (
    semantic_object.c.id,
    semantic_object.c.kind,
    semantic_object.c.name,
    semantic_object.c.label,
    semantic_object.c.status,
    semantic_object.c.domain,
    semantic_object.c.has_proposal,
    semantic_object.c.proposal_status,
    semantic_object.c.official,
    semantic_object.c.proposal,
    semantic_object.c.derived,
    semantic_object.c.version,
)


def _row(found: Any) -> ObjectRow:
    return ObjectRow(
        id=found.id,
        kind=found.kind,
        name=found.name,
        label=found.label or found.name,
        status=found.status,
        domain=found.domain,
        has_proposal=found.has_proposal,
        proposal_status=found.proposal_status,
        official=found.official,
        proposal=found.proposal,
        derived=found.derived,
        version=found.version,
    )


def fetch_object(connection: Connection, object_id: str) -> ObjectRow | None:
    found = connection.execute(select(*_COLUMNS).where(semantic_object.c.id == object_id)).first()
    return _row(found) if found is not None else None


def fetch_objects(connection: Connection, ids: Iterable[str]) -> dict[str, ObjectRow]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = connection.execute(select(*_COLUMNS).where(semantic_object.c.id.in_(wanted)))
    return {row.id: _row(row) for row in rows}


def visible_ids(connection: Connection, sees_drafts: bool) -> list[str]:
    """Ids of every object this caller may see, for close-id suggestions."""
    query = select(semantic_object.c.id)
    if not sees_drafts:
        query = query.where(semantic_object.c.status.in_(PUBLIC_STATUSES))
    return sorted(connection.execute(query).scalars())


def close_ids(connection: Connection, wanted: str, sees_drafts: bool, limit: int = 3) -> list[str]:
    """Up to three ids the caller may see that look like `wanted`."""
    return difflib.get_close_matches(
        wanted, visible_ids(connection, sees_drafts), n=limit, cutoff=0.5
    )


def entity_of(row: ObjectRow) -> Entity | None:
    served = row.definition
    return served if isinstance(served, Entity) else None


def edges(connection: Connection, types: Sequence[str]) -> list[tuple[str, str, str]]:
    """Every stored edge of these types, as `(src, dst, type)`."""
    rows = connection.execute(
        select(object_edge.c.src_id, object_edge.c.dst_id, object_edge.c.type)
        .where(object_edge.c.type.in_(list(types)))
        .order_by(object_edge.c.type, object_edge.c.src_id, object_edge.c.dst_id)
    )
    return [(src, dst, kind) for src, dst, kind in rows]


def metric_closure(edge_list: Iterable[tuple[str, str, str]], metric_id: str) -> list[str]:
    """The metric and every metric it is built from, through `input_of` edges (inputs last)."""
    inputs: dict[str, list[str]] = {}
    for src, dst, kind in edge_list:
        if kind == "input_of":
            inputs.setdefault(dst, []).append(src)
    seen: list[str] = []
    stack = [metric_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.append(current)
        stack.extend(sorted(inputs.get(current, []), reverse=True))
    return seen


def rules_of_metric(edge_list: Sequence[tuple[str, str, str]], metric_id: str) -> list[str]:
    """Rule ids the metric uses itself or through its inputs, sorted."""
    members = set(metric_closure(edge_list, metric_id))
    return sorted({dst for src, dst, kind in edge_list if kind == "uses_rule" and src in members})
