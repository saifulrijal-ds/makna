"""Models for the compiler tests: the sample lending definitions and the small `shop` fixture."""

from __future__ import annotations

import copy
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from makna.compiler.snapshot import Model
from makna.model.definitions import Entity, Metric, parse_definition
from makna.services.examples import model_from_definitions

from . import definition_fixtures as fx
from .definition_fixtures import Files

ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = ROOT / "semantic" / "definitions"
EXAMPLES = ROOT / "semantic" / "schema" / "examples"


@cache
def lending() -> Model:
    """Official sample definitions plus the draft proposal (write_off_amount)."""
    from makna.definitions.loader import load_definitions

    return model_from_definitions(load_definitions(DEFINITIONS))


def shop(*mutations: Callable[[Files], None]) -> Model:
    """The `shop` fixture of the definition tests, with edits applied, as a model."""
    files = copy.deepcopy(fx.base())
    for mutate in mutations:
        mutate(files)
    entities = [d for d in map(parse_definition, _docs(files, "entities")) if isinstance(d, Entity)]
    metrics = [d for d in map(parse_definition, _docs(files, "metrics")) if isinstance(d, Metric)]
    return Model.build(entities, metrics)


def _docs(files: Files, folder: str) -> list[dict[str, Any]]:
    return [doc for path, doc in files.items() if f"/{folder}/" in path]


def metric_doc(files: Files, name: str) -> dict[str, Any]:
    return files[f"shop/metrics/{name}.yaml"]


def entity_doc(files: Files, name: str) -> dict[str, Any]:
    return files[f"shop/entities/{name}.yaml"]


def add_relationship(files: Files, entity: str, name: str, to: str) -> None:
    entity_doc(files, entity).setdefault("relationships", []).append(
        {
            "name": name,
            "to": to,
            "keys": [{"from": f"{name}_id", "to": f"{to}_id"}],
            "description": f"The {name} of the row.",
        }
    )
