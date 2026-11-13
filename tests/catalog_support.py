"""Helpers shared by the WP-06 tests: the sample export, edited copies of it, small definitions."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from makna.definitions.loader import official_objects
from makna.model.definitions import Entity, GlossaryTerm, parse_definition

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "data" / "catalog" / "lending.catalog.yaml"
DEFINITIONS = ROOT / "semantic" / "definitions"
TAG_MAP = {
    "PII.Sensitive": "personal",
    "PersonalData.Personal": "personal",
    "PII.NonSensitive": "confidential",
}


def export_data() -> dict[str, Any]:
    data = yaml.safe_load(EXPORT.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def write_export(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def export_without_column(path: Path, table: str, column: str) -> Path:
    """A copy of the sample export with one column removed."""
    data = copy.deepcopy(export_data())
    for dataset in data["datasets"]:
        if dataset["name"] == table:
            before = len(dataset["columns"])
            dataset["columns"] = [c for c in dataset["columns"] if c["name"] != column]
            assert len(dataset["columns"]) == before - 1
    return write_export(path, data)


def sample_objects() -> tuple[list[Entity], list[GlossaryTerm]]:
    """Official entities and glossary terms of the sample definitions."""
    found = official_objects(DEFINITIONS)
    assert not found.invalid
    return found.entities, found.terms


def make_entity(
    name: str = "thing",
    table: str = "lending.core.things",
    *,
    sensitivity: str = "internal",
    primary_key: tuple[str, ...] = ("thing_id",),
    dimensions: list[dict[str, Any]] | None = None,
    measures: list[dict[str, Any]] | None = None,
    rules: list[dict[str, Any]] | None = None,
    relationships: list[dict[str, Any]] | None = None,
) -> Entity:
    """A minimal valid entity; the dimension, measure, rule and relationship lists are optional."""
    doc: dict[str, Any] = {
        "apiVersion": "makna/v1",
        "kind": "entity",
        "name": name,
        "label": name.title(),
        "description": f"The {name} entity of the test domain, written for a reader.",
        "owner": {"team": "test"},
        "status": "approved",
        "sensitivity": sensitivity,
        "domain": "lending",
        "synonyms": {"en": ["x"], "id": ["y"]},
        "source": {"table": table},
        "primary_key": list(primary_key),
        "dimensions": dimensions or [],
    }
    if measures:
        doc["measures"] = measures
    if rules:
        doc["rules"] = rules
    if relationships:
        doc["relationships"] = relationships
    parsed = parse_definition(doc)
    assert isinstance(parsed, Entity)
    return parsed


def dimension(name: str, expr: str | None = None, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "type": "categorical",
        "description": f"The {name} of the thing.",
        **fields,
    }
    if expr is not None:
        out["expr"] = expr
    return out


def make_term(name: str = "npf", catalog_term: str | None = None) -> GlossaryTerm:
    doc: dict[str, Any] = {
        "apiVersion": "makna/v1",
        "kind": "glossary_term",
        "name": name,
        "label": name.upper(),
        "description": f"The business term {name}, written for a reader.",
        "owner": {"team": "test"},
        "status": "approved",
        "sensitivity": "internal",
        "domain": "lending",
        "synonyms": {"en": ["x"], "id": ["y"]},
    }
    if catalog_term is not None:
        doc["catalog_term"] = catalog_term
    parsed = parse_definition(doc)
    assert isinstance(parsed, GlossaryTerm)
    return parsed


def export_with_tags(path: Path, tags: dict[tuple[str, str], list[str]]) -> Path:
    """A copy of the sample export where the named columns carry the given catalog tags
    (replacing what they had). Keys are `(table name, column name)`."""
    data = copy.deepcopy(export_data())
    seen: set[tuple[str, str]] = set()
    for dataset in data["datasets"]:
        for column in dataset["columns"]:
            key = (dataset["name"], column["name"])
            if key in tags:
                column["tags"] = list(tags[key])
                seen.add(key)
    assert seen == set(tags), f"columns not found: {set(tags) - seen}"
    return write_export(path, data)
