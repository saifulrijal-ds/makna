"""Sensitivity levels, from least to most strict (ARCHITECTURE section 7.6)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from makna.model.definitions import Dimension, Entity

LEVELS: tuple[str, ...] = ("public", "internal", "confidential", "personal")


def rank(level: str) -> int:
    """Position of `level` in `LEVELS`; raises ValueError for an unknown level."""
    try:
        return LEVELS.index(level)
    except ValueError:
        raise ValueError(f"unknown sensitivity level {level!r}") from None


def stricter(a: str, b: str) -> str:
    """The stricter of two levels."""
    return a if rank(a) >= rank(b) else b


def derive_from_tags(tags: Iterable[str], tag_map: Mapping[str, str]) -> str | None:
    """The strictest level the catalog tags map to, or None when no tag is in the map."""
    found: str | None = None
    for tag in tags:
        level = tag_map.get(tag)
        if level is not None:
            found = level if found is None else stricter(found, level)
    return found


def columns_read(dimension: Dimension) -> tuple[str, ...]:
    """Names of the columns a dimension's expression reads, as written.

    A dimension with no `expr` reads the column of its own name. An expression that does not
    parse reads the dimension's own name: validation (V05) rejects such files, and a broken
    expression elsewhere must not stop a request that never uses it.
    """
    if dimension.expr is None:
        return (dimension.name,)
    try:
        tree = sqlglot.parse_one(dimension.expr, into=exp.Condition)
    except SqlglotError:
        return (dimension.name,)
    return tuple(dict.fromkeys(c.name for c in tree.find_all(exp.Column)))


def effective_level(
    entity: Entity, dimension: Dimension, derived: Mapping[tuple[str, str], str]
) -> str:
    """The stricter of the dimension's level (the entity's, if it sets none) and the levels the
    catalog derives for the columns its expression reads (section 7.6).

    `derived` is keyed `(engine_fqn, column)` in lower case, as the catalog stores it.
    """
    level: str = dimension.sensitivity or entity.sensitivity
    table = entity.source.table.lower()
    for column in columns_read(dimension):
        found = derived.get((table, column.lower()))
        if found is not None:
            level = stricter(level, found)
    return level
