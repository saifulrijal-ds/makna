"""Generator of the OpenMetadata fixtures (CON-2): the sample catalog as OpenMetadata answers.

Reads `data/catalog/lending.catalog.yaml` and writes the JSON bodies that `GET /api/v1/tables`
and `GET /api/v1/glossaryTerms` return in OpenMetadata 2.0.7, split into pages, into
`tests/unit/fixtures/openmetadata/`. The shapes were written from the release's published JSON
schemas, not recorded from a live server (see the README beside the fixtures). Run

    uv run python -m tests.openmetadata_fixtures

after a change of the sample catalog or of this file; a unit test fails if the committed files
differ from what this script writes. Files named `hand_*` are written by hand and not touched.

This is the first of the three mappings the contract test compares: export to OpenMetadata JSON
(here), OpenMetadata JSON to models (`makna.catalog.openmetadata`), export to models (the file
connector). It shares no code with the connector on purpose: the type names and the constraint
names below are written out, not imported.
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "data" / "catalog" / "lending.catalog.yaml"
OUT = ROOT / "tests" / "unit" / "fixtures" / "openmetadata"

RELEASE = "2.0.7"
SERVICE = "lending_sample"
DATABASE = "lending_prod"
GLOSSARY = "lending"
TABLE_PAGE_SIZE = 4
TERM_PAGE_SIZE = 5
UPDATED_AT_MS = 1782777600000  # 2026-06-30T00:00:00Z, the export date of the sample catalog
NAMESPACE = uuid.UUID("5d3c1e0a-6f1b-4d52-9a57-0c1f0b6a7d11")

# normalized type (ARCHITECTURE 6.1) to the OpenMetadata `dataType` that carries it
OM_TYPES = {
    "string": "VARCHAR",
    "integer": "INT",
    "decimal": "DECIMAL",
    "float": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "other": "UNKNOWN",
}


def entity_id(kind: str, name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{kind}:{name}"))


def reference(kind: str, name: str, fqn: str | None = None) -> dict[str, Any]:
    return {
        "id": entity_id(kind, fqn or name),
        "type": kind,
        "name": name,
        "fullyQualifiedName": fqn or name,
        "deleted": False,
    }


def tag_label(tag: str) -> dict[str, Any]:
    return {"tagFQN": tag, "source": "Classification", "labelType": "Manual", "state": "Confirmed"}


def column_entity(table_fqn: str, column: dict[str, Any], position: int) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "name": column["name"],
        "dataType": OM_TYPES[column["data_type"]],
        "dataTypeDisplay": column["native_type"],
    }
    if column.get("description"):
        entity["description"] = column["description"]
    entity["fullyQualifiedName"] = f"{table_fqn}.{column['name']}"
    entity["tags"] = [tag_label(t) for t in column.get("tags", [])]
    if column.get("is_primary_key"):
        entity["constraint"] = "PRIMARY_KEY"
    elif column.get("nullable", True) is False:
        entity["constraint"] = "NOT_NULL"
    entity["ordinalPosition"] = position + 1
    return entity


def table_entity(dataset: dict[str, Any]) -> dict[str, Any]:
    _, schema, name = dataset["engine_fqn"].split(".")
    schema_fqn = f"{SERVICE}.{DATABASE}.{schema}"
    table_fqn = f"{schema_fqn}.{name}"
    entity: dict[str, Any] = {
        "id": entity_id("table", table_fqn),
        "name": name,
        "fullyQualifiedName": table_fqn,
    }
    if dataset.get("description"):
        entity["description"] = dataset["description"]
    entity.update(
        {
            "version": 0.1,
            "updatedAt": UPDATED_AT_MS,
            "updatedBy": "admin",
            "tableType": "Regular",
            "columns": [
                column_entity(table_fqn, c, position)
                for position, c in enumerate(dataset["columns"])
            ],
            "owners": [reference(o["type"], o["name"]) for o in dataset.get("owners", [])],
            "tags": [tag_label(t) for t in dataset.get("tags", [])],
            "service": reference("databaseService", SERVICE),
            "serviceType": "Postgres",
            "database": reference("database", DATABASE, f"{SERVICE}.{DATABASE}"),
            "databaseSchema": reference("databaseSchema", schema, schema_fqn),
            "deleted": False,
        }
    )
    return entity


def term_entity(term: dict[str, Any]) -> dict[str, Any]:
    name = term["name"]
    entity: dict[str, Any] = {
        "id": entity_id("glossaryTerm", term["catalog_id"]),
        "name": name,
        "displayName": name,
        "description": term.get("definition", ""),
        "fullyQualifiedName": f"{GLOSSARY}.{name}",
        "synonyms": list(term.get("synonyms", [])),
        "glossary": reference("glossary", GLOSSARY),
        "relatedTerms": [
            {"relationType": "relatedTo", "term": reference("glossaryTerm", related)}
            for related in term.get("related_terms", [])
        ],
        "tags": [tag_label(t) for t in term.get("tags", [])],
        "version": 0.1,
        "updatedAt": UPDATED_AT_MS,
        "updatedBy": "admin",
        "entityStatus": "Approved",
        "deleted": False,
    }
    return entity


def cursor(entity: dict[str, Any]) -> str:
    """An opaque `after` cursor in the style of the server: base64 of a small JSON object."""
    raw = json.dumps({"name": entity["name"], "id": entity["id"]}, separators=(",", ":"))
    return base64.b64encode(raw.encode("ascii")).decode("ascii")


def pages(entities: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    """The listing bodies: `{"data": [...], "paging": {"total": n, "after": ...}}`."""
    chunks = [entities[i : i + size] for i in range(0, len(entities), size)] or [[]]
    bodies = []
    for number, chunk in enumerate(chunks):
        paging: dict[str, Any] = {"total": len(entities)}
        if number > 0:
            paging["before"] = cursor(chunks[number - 1][0])
        if number < len(chunks) - 1:
            paging["after"] = cursor(chunk[-1])
        bodies.append({"data": chunk, "paging": paging})
    return bodies


def dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def render() -> dict[str, str]:
    """File name to text of every generated fixture."""
    export = yaml.safe_load(EXPORT.read_text(encoding="utf-8"))
    files: dict[str, str] = {}
    tables = [table_entity(d) for d in export["datasets"]]
    for number, body in enumerate(pages(tables, TABLE_PAGE_SIZE), start=1):
        files[f"tables_page_{number}.json"] = dump(body)
    terms = [term_entity(t) for t in export["glossary_terms"]]
    for number, body in enumerate(pages(terms, TERM_PAGE_SIZE), start=1):
        files[f"glossary_terms_page_{number}.json"] = dump(body)
    return files


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("tables_page_*.json"):
        stale.unlink()
    for stale in OUT.glob("glossary_terms_page_*.json"):
        stale.unlink()
    for name, text in render().items():
        (OUT / name).write_text(text, encoding="utf-8")
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
