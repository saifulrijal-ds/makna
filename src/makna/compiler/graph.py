"""Entities and relationships as a join graph, and the path rule of section 7.3.

Relationships point from the "many" side to the "one" side, and a path may only walk them
forward, so no join can multiply the rows of the measure's entity. To reach entity B from A:

1. a path pinned by the metric (`join_paths`) wins;
2. otherwise take every path of up to 4 steps that visits no entity twice; none is
   `NO_JOIN_PATH`;
3. exactly one shortest path is used;
4. several equally short paths are `AMBIGUOUS_JOIN_PATH`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from makna.compiler.snapshot import Model
from makna.model.request import ErrorCode, QueryError

MAX_STEPS = 4


@dataclass(frozen=True, order=True)
class JoinStep:
    """One relationship walked forward: `entity.relationship`, arriving at `to`."""

    entity: str
    relationship: str
    to: str

    @property
    def name(self) -> str:
        return f"{self.entity}.{self.relationship}"

    def __str__(self) -> str:
        return self.name


JoinPath = tuple[JoinStep, ...]


def path_names(path: JoinPath) -> list[str]:
    """A path as the names used in definitions and in error details."""
    return [step.name for step in path]


def shortest_paths_from(
    model: Model, start: str, max_steps: int = MAX_STEPS
) -> dict[str, tuple[JoinPath, ...]]:
    """For every entity reachable from `start`, all of its shortest paths (rule 7.3, steps 2-4).

    `start` maps to the empty path. Paths are listed in a fixed order, so results are repeatable.
    """
    best: dict[str, list[JoinPath]] = {start: [()]}

    def walk(entity_name: str, path: JoinPath, visited: frozenset[str]) -> None:
        entity = model.entities.get(entity_name)
        if entity is None or len(path) >= max_steps:
            return
        for relationship in entity.relationships or []:
            target = relationship.to
            if target in visited or target not in model.entities:
                continue
            new = (*path, JoinStep(entity_name, relationship.name, target))
            found = best.setdefault(target, [])
            if not found or len(new) < len(found[0]):
                found[:] = [new]
            elif len(new) == len(found[0]):
                found.append(new)
            walk(target, new, visited | {target})

    walk(start, (), frozenset({start}))
    return {name: tuple(sorted(paths)) for name, paths in best.items()}


def find_paths(
    model: Model, start: str, end: str, max_steps: int = MAX_STEPS
) -> tuple[JoinPath, ...]:
    """The shortest paths from `start` to `end`: none, one, or several equally short."""
    return shortest_paths_from(model, start, max_steps).get(end, ())


def resolve_path(
    model: Model,
    start: str,
    end: str,
    pinned: Mapping[str, Sequence[str]] | None = None,
) -> JoinPath:
    """The one path to use from `start` to `end`, or a `NO_JOIN_PATH`/`AMBIGUOUS_JOIN_PATH` error.

    `pinned` is the metric's `join_paths`: target entity to a chain of `entity.relationship`.
    """
    if start == end:
        return ()
    chain = (pinned or {}).get(end)
    if chain:
        return _pinned_path(model, start, end, chain)
    paths = find_paths(model, start, end)
    if not paths:
        raise QueryError(
            ErrorCode.NO_JOIN_PATH,
            f"No approved join path of up to {MAX_STEPS} steps leads from {start} to {end}. "
            f"Relationships only lead from the 'many' side to the 'one' side; choose a "
            f"dimension of an entity that {start} can reach.",
            {"from": start, "to": end, "candidate_paths": []},
        )
    if len(paths) > 1:
        options = [path_names(p) for p in paths]
        raise QueryError(
            ErrorCode.AMBIGUOUS_JOIN_PATH,
            f"{len(paths)} equally short join paths lead from {start} to {end}: "
            f"{', '.join(' > '.join(o) for o in options)}. The metric must pin one in join_paths.",
            {"from": start, "to": end, "candidate_paths": options},
        )
    return paths[0]


def _pinned_path(model: Model, start: str, end: str, chain: Sequence[str]) -> JoinPath:
    steps: list[JoinStep] = []
    current = start
    for name in chain:
        found = model.relationship(name)
        if found is None or found[0].name != current:
            raise _bad_pin(start, end, chain, f"step {name} does not continue from {current}")
        steps.append(JoinStep(found[0].name, found[1].name, found[1].to))
        current = found[1].to
    if current != end:
        raise _bad_pin(start, end, chain, f"the chain ends at {current}")
    return tuple(steps)


def _bad_pin(start: str, end: str, chain: Sequence[str], problem: str) -> QueryError:
    return QueryError(
        ErrorCode.NO_JOIN_PATH,
        f"The pinned join path from {start} to {end} is not valid: {problem}.",
        {"from": start, "to": end, "candidate_paths": [], "pinned": list(chain)},
    )
