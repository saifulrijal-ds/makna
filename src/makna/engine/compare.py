"""Catalog against engine: which tables and columns the two disagree on.

A pure function over the catalog copy's tables and the engine's tables, so it needs no database.
Only names are compared. Column types are not: a catalog writes `varchar` where PostgreSQL says
`character varying`, and a difference of that kind is not a missing column. Names are compared
without regard to case, as the catalog stores column names in lower case.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from makna.engine.base import EngineTable

DifferenceKind = Literal["missing_table", "missing_column", "extra_table", "extra_column"]


@dataclass(frozen=True)
class Difference:
    """One disagreement. `missing_*` means the catalog has it and the engine does not;
    `extra_*` means the engine has it and the catalog does not."""

    kind: DifferenceKind
    schema: str
    table: str
    column: str | None = None

    @property
    def name(self) -> str:
        base = f"{self.schema}.{self.table}"
        return base if self.column is None else f"{base}.{self.column}"


def schema_and_table(engine_fqn: str) -> tuple[str, str]:
    """The schema and table of `catalog.schema.table`. The catalog part is not compared: one
    connection reads one database."""
    parts = engine_fqn.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(_bad_fqn(engine_fqn))
    return parts[1].lower(), parts[2].lower()


def catalog_schemas(catalog: Mapping[str, Iterable[str]]) -> list[str]:
    """The schemas the catalog tables are in, sorted: the ones to introspect."""
    return sorted({schema_and_table(fqn)[0] for fqn in catalog})


def compare(
    catalog: Mapping[str, Iterable[str]], engine_tables: Sequence[EngineTable]
) -> list[Difference]:
    """The differences, sorted by table and column.

    `catalog` maps each active catalog table's `engine_fqn` to its column names. Engine tables
    in a schema the catalog has no table in are not reported (they are outside the scope), so
    pass the tables of the schemas `catalog_schemas` returns.
    """
    wanted: dict[tuple[str, str], set[str]] = {}
    for fqn, columns in catalog.items():
        wanted[schema_and_table(fqn)] = {c.lower() for c in columns}
    schemas = {schema for schema, _ in wanted}
    found: dict[tuple[str, str], set[str]] = {
        (t.schema.lower(), t.name.lower()): {c.name.lower() for c in t.columns}
        for t in engine_tables
        if t.schema.lower() in schemas
    }
    differences: list[Difference] = []
    for (schema, table), columns in wanted.items():
        if (schema, table) not in found:
            differences.append(Difference("missing_table", schema, table))
            continue
        for column in columns - found[(schema, table)]:
            differences.append(Difference("missing_column", schema, table, column))
        for column in found[(schema, table)] - columns:
            differences.append(Difference("extra_column", schema, table, column))
    for schema, table in found.keys() - wanted.keys():
        differences.append(Difference("extra_table", schema, table))
    return sorted(differences, key=lambda d: (d.schema, d.table, d.column or "", d.kind))


def _bad_fqn(engine_fqn: str) -> str:
    return f"{engine_fqn!r} is not catalog.schema.table"
