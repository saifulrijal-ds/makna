"""`list_related`: the objects near one object (ARCHITECTURE section 12).

Relations come from `object_edge`, which the indexer writes. The relation of each hit says what
the hit is to the object asked about:

| relation | the hit is ... |
|---|---|
| `input_of` | an input of the metric |
| `built_on` | a metric built from the metric |
| `uses_rule` | a rule the metric uses, itself or through its inputs |
| `used_by` | a metric that uses the rule, itself or through its inputs |
| `explains` | a business term that explains the object |
| `explained` | an object the business term explains |
| `measured_on` | an entity the metric is measured on (through its inputs) |
| `measures` | a metric measured on the entity |
| `same_entity` | another metric measured on one of the same entities |
| `joins_to` | an entity the entity joins to |
| `joined_from` | an entity that joins to the entity |
| `replaced_by` | the replacement of the deprecated object |
| `replaces` | a deprecated object the object replaces |

A hit the caller may not see is never listed, and a deprecated hit only when asked for (or when
it is the replacement relation itself). At most 20 hits, ordered by relation as in the table, then
by id.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Connection

from makna.model.definitions import Entity
from makna.services import objects
from makna.services.objects import ObjectRow
from makna.services.views import RelatedItem, cut

RELATIONS: tuple[str, ...] = (
    "replaced_by",
    "replaces",
    "input_of",
    "built_on",
    "uses_rule",
    "used_by",
    "explains",
    "explained",
    "measured_on",
    "measures",
    "same_entity",
    "joins_to",
    "joined_from",
)
MAX_RELATED = 20
EDGE_TYPES = ("input_of", "measured_on", "joins_to", "uses_rule", "explains", "replaced_by")

Edges = Sequence[tuple[str, str, str]]


def _entities_of(edges: Edges, metric_id: str) -> set[str]:
    members = set(objects.metric_closure(edges, metric_id))
    return {dst for src, dst, kind in edges if kind == "measured_on" and src in members}


def related_pairs(edges: Edges, subject: str) -> list[tuple[str, str]]:
    """`(object id, relation)` for every object related to `subject`, before visibility."""
    pairs: list[tuple[str, str]] = []
    kind = subject.partition(":")[0]
    for src, dst, edge in edges:
        if edge == "replaced_by":
            if src == subject:
                pairs.append((dst, "replaced_by"))
            elif dst == subject:
                pairs.append((src, "replaces"))
        elif edge == "explains":
            if dst == subject:
                pairs.append((src, "explains"))
            elif src == subject:
                pairs.append((dst, "explained"))
        elif edge == "input_of":
            if dst == subject:
                pairs.append((src, "input_of"))
            elif src == subject:
                pairs.append((dst, "built_on"))
        elif edge == "joins_to":
            if src == subject:
                pairs.append((dst, "joins_to"))
            elif dst == subject:
                pairs.append((src, "joined_from"))
    metrics = {s for s, _, e in edges if e in ("measured_on", "uses_rule")}
    metrics |= {x for s, d, e in edges if e == "input_of" for x in (s, d)}
    if kind == "metric":
        pairs.extend((rule, "uses_rule") for rule in objects.rules_of_metric(edges, subject))
        pairs.extend((entity, "measured_on") for entity in sorted(_entities_of(edges, subject)))
        mine = _entities_of(edges, subject)
        pairs.extend(
            (other, "same_entity")
            for other in sorted(metrics - {subject})
            if mine & _entities_of(edges, other)
        )
    elif kind == "rule":
        pairs.extend(
            (metric, "used_by")
            for metric in sorted(metrics)
            if subject in objects.rules_of_metric(edges, metric)
        )
    elif kind == "entity":
        pairs.extend(
            (metric, "measures")
            for metric in sorted(metrics)
            if subject in _entities_of(edges, metric)
        )
    return list(dict.fromkeys(pairs))


def list_related(
    connection: Connection,
    subject: str,
    *,
    sees_drafts: bool,
    include_deprecated: bool,
    relation: str | None,
) -> list[RelatedItem]:
    """At most 20 related objects the caller may see, with one-line descriptions."""
    pairs = related_pairs(objects.edges(connection, EDGE_TYPES), subject)
    if relation is not None:
        pairs = [pair for pair in pairs if pair[1] == relation]
    owners = {_owner_id(object_id) for object_id, _ in pairs}
    rows = objects.fetch_objects(connection, owners)
    items: list[tuple[int, str, RelatedItem]] = []
    for object_id, how in pairs:
        row = rows.get(_owner_id(object_id))
        if row is None or not row.visible_to(sees_drafts):
            continue
        if (
            row.status == "deprecated"
            and not include_deprecated
            and how
            not in (
                "replaced_by",
                "replaces",
            )
        ):
            continue
        shown = _describe(object_id, how, row)
        if shown is not None:
            items.append((RELATIONS.index(how), object_id, shown))
    items.sort(key=lambda item: (item[0], item[1]))
    return [item for _, _, item in items[:MAX_RELATED]]


def _owner_id(object_id: str) -> str:
    """The row that holds an object: a rule is part of its entity."""
    kind, _, name = object_id.partition(":")
    return f"entity:{name.partition('.')[0]}" if kind in ("rule", "dimension") else object_id


def _describe(object_id: str, relation: str, row: ObjectRow) -> RelatedItem | None:
    kind, _, name = object_id.partition(":")
    if kind in ("rule", "dimension"):
        entity = row.definition
        if not isinstance(entity, Entity):
            return None
        part = name.partition(".")[2]
        rule = next((r for r in entity.rules or [] if r.name == part), None)
        if rule is None:
            return None
        return RelatedItem(
            id=object_id,
            label=rule.label or rule.name.replace("_", " "),
            kind="rule",
            relation=relation,
            description=cut(rule.description),
        )
    return RelatedItem(
        id=row.id,
        label=row.label,
        kind=row.kind,
        relation=relation,
        description=cut(row.definition.description),
    )
