"""The reference check (ARCHITECTURE section 5.3, SEM-4): definitions against the synced catalog.

Codes:

- R01  `source.table` of an entity is not in the synced catalog.
- R02  A column read by a primary key, relationship key, `expr`, or `filter` is not in the table.
- R03  A dimension reads a column whose catalog tags map to a stricter sensitivity than the
       dimension declares.
- R04  `catalog_term` of a glossary term does not exist in the catalog (matched against the
       catalog id or the name of a catalog glossary term).

`find_issues` is a pure function of model objects and a `CatalogView`. `plan_changes` compares
its findings with the open rows of `reference_issue`; `apply_changes` writes the difference.
An issue is identified by (object, part, code): while it stays true its row stays open and
untouched, and when it stops being true the row is closed (`closed_at` set), not deleted.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlglot
from sqlalchemy import Connection, and_, insert, select, update
from sqlglot import exp
from sqlglot.errors import SqlglotError

from makna.index.tables import catalog_column, catalog_dataset, catalog_glossary_term
from makna.index.tables import reference_issue as issue_table
from makna.model.definitions import Dimension, Entity, GlossaryTerm
from makna.model.sensitivity import rank


@dataclass(frozen=True)
class Finding:
    object_id: str
    part: str
    code: str
    message: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.object_id, self.part, self.code)


@dataclass(frozen=True)
class CatalogView:
    """What the check needs from the synced catalog."""

    tables: Mapping[str, Mapping[str, str | None]] = field(default_factory=dict)
    """engine_fqn -> {lower-case column name -> derived sensitivity or None}, active tables only."""
    terms: frozenset[str] = frozenset()
    """Catalog ids and names of the catalog glossary terms."""


@dataclass(frozen=True)
class Changes:
    to_open: tuple[Finding, ...] = ()
    to_close: tuple[int, ...] = ()
    """Ids of open `reference_issue` rows that are no longer true."""
    to_reword: tuple[tuple[int, str], ...] = ()
    """(id, new message) for open rows that are still true but now say something else."""

    @property
    def opened(self) -> int:
        return len(self.to_open)

    @property
    def closed(self) -> int:
        return len(self.to_close)

    @property
    def reworded(self) -> int:
        return len(self.to_reword)

    @property
    def is_empty(self) -> bool:
        return not (self.to_open or self.to_close or self.to_reword)


def columns_read(sql: str | None) -> set[str]:
    """Lower-case names of the columns an SQL expression reads (empty if it does not parse)."""
    if not sql:
        return set()
    try:
        tree = sqlglot.parse_one(sql, into=exp.Condition)
    except SqlglotError:
        return set()
    return {c.name.lower() for c in tree.find_all(exp.Column)}


def _declared(entity: Entity, dimension: Dimension) -> str:
    return dimension.sensitivity or entity.sensitivity


def _missing_columns(
    object_id: str, part: str, needed: Iterable[str], fqn: str, where: Mapping[str, str | None]
) -> Finding | None:
    absent = sorted({name.lower() for name in needed} - set(where))
    if not absent:
        return None
    return Finding(object_id, part, "R02", f"column {', '.join(absent)} is not in table {fqn}")


def _entity_findings(
    entity: Entity, by_name: Mapping[str, Entity], view: CatalogView
) -> list[Finding]:
    object_id = f"entity:{entity.name}"
    table = entity.source.table
    columns = view.tables.get(table)
    if columns is None:
        return [Finding(object_id, "source.table", "R01", f"table {table} is not in the catalog")]
    findings: list[Finding] = []

    def report_missing(
        part: str,
        needed: Iterable[str],
        fqn: str = table,
        where: Mapping[str, str | None] = columns,
    ) -> None:
        if finding := _missing_columns(object_id, part, needed, fqn, where):
            findings.append(finding)

    report_missing("primary_key", entity.primary_key)
    for dimension in entity.dimensions:
        read = columns_read(dimension.expr if dimension.expr is not None else dimension.name)
        part = f"dimension:{dimension.name}"
        report_missing(part, read)
        declared = _declared(entity, dimension)
        stricter = []
        for column in sorted(read & set(columns)):
            level = columns[column]
            if level is not None and rank(level) > rank(declared):
                stricter.append(f"{column} ({level})")
        if stricter:
            findings.append(
                Finding(
                    object_id,
                    part,
                    "R03",
                    f"dimension {dimension.name} is declared {declared} but reads "
                    f"{', '.join(stricter)} according to the catalog tags",
                )
            )
    for measure in entity.measures or []:
        read = columns_read(measure.expr) | columns_read(measure.filter)
        report_missing(f"measure:{measure.name}", read)
    for rule in entity.rules or []:
        report_missing(f"rule:{rule.name}", columns_read(rule.expr))
    for relationship in entity.relationships or []:
        part = f"relationship:{relationship.name}"
        report_missing(part, (k.from_ for k in relationship.keys))
        target = by_name.get(relationship.to)
        target_columns = view.tables.get(target.source.table) if target else None
        if target is not None and target_columns is not None:
            report_missing(
                part, (k.to for k in relationship.keys), target.source.table, target_columns
            )
    return findings


def find_issues(
    entities: Iterable[Entity], terms: Iterable[GlossaryTerm], view: CatalogView
) -> list[Finding]:
    """All reference issues R01 to R04 of these objects against this catalog."""
    entity_list = list(entities)
    by_name = {e.name: e for e in entity_list}
    findings: list[Finding] = []
    for entity in entity_list:
        findings.extend(_entity_findings(entity, by_name, view))
    for term in terms:
        if term.catalog_term is not None and term.catalog_term not in view.terms:
            findings.append(
                Finding(
                    f"glossary_term:{term.name}",
                    "catalog_term",
                    "R04",
                    f"catalog term {term.catalog_term!r} is not in the catalog",
                )
            )
    return _merge(findings)


def _merge(findings: list[Finding]) -> list[Finding]:
    """One finding per (object, part, code); messages of repeated keys are joined."""
    merged: dict[tuple[str, str, str], Finding] = {}
    for finding in findings:
        existing = merged.get(finding.key)
        if existing is None:
            merged[finding.key] = finding
        else:
            merged[finding.key] = Finding(*finding.key, f"{existing.message}; {finding.message}")
    return list(merged.values())


def load_view(connection: Connection) -> CatalogView:
    """The catalog view from the catalog tables (datasets not marked removed)."""
    tables: dict[str, dict[str, str | None]] = {}
    rows = connection.execute(
        select(
            catalog_dataset.c.engine_fqn,
            catalog_column.c.name,
            catalog_column.c.derived_sensitivity,
        )
        .select_from(
            catalog_dataset.outerjoin(
                catalog_column, catalog_column.c.dataset_id == catalog_dataset.c.id
            )
        )
        .where(catalog_dataset.c.removed_at.is_(None))
    )
    for fqn, name, derived in rows:
        columns = tables.setdefault(fqn, {})
        if name is not None:
            columns[name.lower()] = derived
    terms: set[str] = set()
    for catalog_id, name in connection.execute(
        select(catalog_glossary_term.c.catalog_id, catalog_glossary_term.c.name)
    ):
        terms.update(value for value in (catalog_id, name) if value)
    return CatalogView(tables, frozenset(terms))


def open_issues(connection: Connection) -> list[Finding]:
    """The open issues, in the order they were opened."""
    return [
        Finding(r.object_id, r.part, r.code, r.message)
        for r in connection.execute(
            select(issue_table).where(issue_table.c.closed_at.is_(None)).order_by(issue_table.c.id)
        )
    ]


def plan_changes(
    connection: Connection,
    entities: Iterable[Entity],
    terms: Iterable[GlossaryTerm],
    keep: Collection[str] = (),
) -> Changes:
    """Compare the findings against the catalog tables with the open rows. Writes nothing.

    Open issues of the objects in `keep` (object ids whose definition file could not be read)
    are left exactly as they are.
    """
    found = {f.key: f for f in find_issues(entities, terms, load_view(connection))}
    still_open: set[tuple[str, str, str]] = set()
    to_close: list[int] = []
    to_reword: list[tuple[int, str]] = []
    for row in connection.execute(
        select(issue_table).where(issue_table.c.closed_at.is_(None)).order_by(issue_table.c.id)
    ):
        if row.object_id in keep:
            continue
        key = (row.object_id, row.part, row.code)
        current = found.get(key)
        if current is None or key in still_open:
            to_close.append(row.id)
            continue
        still_open.add(key)
        if current.message != row.message:
            to_reword.append((row.id, current.message))
    to_open = tuple(f for k, f in found.items() if k not in still_open)
    return Changes(to_open, tuple(to_close), tuple(to_reword))


def apply_changes(connection: Connection, changes: Changes, now: datetime | None = None) -> None:
    moment = now or datetime.now(UTC)
    if changes.to_open:
        connection.execute(
            insert(issue_table),
            [
                {
                    "object_id": f.object_id,
                    "part": f.part,
                    "code": f.code,
                    "message": f.message,
                    "opened_at": moment,
                }
                for f in changes.to_open
            ],
        )
    for issue_id in changes.to_close:
        connection.execute(
            update(issue_table)
            .where(and_(issue_table.c.id == issue_id, issue_table.c.closed_at.is_(None)))
            .values(closed_at=moment)
        )
    for issue_id, message in changes.to_reword:
        connection.execute(
            update(issue_table).where(issue_table.c.id == issue_id).values(message=message)
        )


def check(
    connection: Connection,
    entities: Iterable[Entity],
    terms: Iterable[GlossaryTerm],
    now: datetime | None = None,
    keep: Collection[str] = (),
) -> Changes:
    """Run the reference check and write `reference_issue`. Returns what changed."""
    entity_list, term_list = list(entities), list(terms)
    changes = plan_changes(connection, entity_list, term_list, keep)
    apply_changes(connection, changes, now)
    return changes
