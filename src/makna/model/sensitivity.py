"""Sensitivity levels, from least to most strict (ARCHITECTURE section 7.6)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

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
