"""Models for the compiler tests: the sample lending definitions and the small `shop` fixture."""

from __future__ import annotations

import copy
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from sqlglot import exp

from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import build_tree
from makna.model.definitions import Entity, Metric, parse_definition
from makna.model.request import QueryRequest
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


def request(metric: str, **fields: Any) -> QueryRequest:
    return QueryRequest.model_validate({"metric": metric, **fields})


def tree_of(model: Model, metric: str, **fields: Any) -> exp.Select:
    """The dialect-neutral tree for a request, planned with the status gate on."""
    return build_tree(model, plan_request(model, request(metric, **fields)))


def source_tables(select: exp.Select) -> set[str]:
    """Names of the tables this SELECT reads directly (its FROM and JOINs, not subqueries)."""
    sources = [select.args["from_"].this] if select.args.get("from_") else []
    sources.extend(join.this for join in select.args.get("joins") or [])
    return {s.name for s in sources if isinstance(s, exp.Table)}
