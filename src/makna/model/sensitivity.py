"""Sensitivity levels, from least to most strict (ARCHITECTURE section 7.6)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from makna.model.definitions import Dimension, Entity
from makna.model.expressions import ExpressionError, dimension_columns

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


def effective_level(
    entity: Entity, dimension: Dimension, derived: Mapping[tuple[str, str], str]
) -> str:
    """The stricter of the dimension's level (the entity's, if it sets none) and the levels the
    catalog derives for the columns its expression reads (section 7.6).

    `derived` is keyed `(engine_fqn, column)` in lower case, as the catalog stores it. A
    dimension whose expression does not parse reads columns nobody can name, so it is as
    sensitive as it can be: `personal`.
    """
    level: str = dimension.sensitivity or entity.sensitivity
    table = entity.source.table.lower()
    try:
        read = dimension_columns(dimension)
    except ExpressionError:
        return LEVELS[-1]
    for column in read:
        found = derived.get((table, column))
        if found is not None:
            level = stricter(level, found)
    return level
