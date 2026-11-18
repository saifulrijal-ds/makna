"""Load the sample catalog into an OpenMetadata server (CON-2).

    MAKNA_OPENMETADATA_TOKEN=<token of a user or bot that may write> \\
      uv run python -m deploy.seed_openmetadata [--url http://localhost:8585]

Reads `data/catalog/lending.catalog.yaml` and creates, through the REST API of OpenMetadata
2.0.7 with `httpx`: the database service `lending_sample` with its database and schemas, the
teams and users that own tables, the classifications and tags the columns carry, the tables with
their columns, and a glossary with its terms. Every call is a `PUT`, which OpenMetadata treats
as create-or-update, so a second run changes nothing. It lives outside `src/makna` like
`deploy/init.py`, because it is a development tool that reads `data/`.

The request bodies are built by pure functions (`table_body` and the others), which the unit
tests check against the shapes of the connector's fixtures. The script itself has not been run
against a server: WP-21 manual criterion 4 is its first real use. The token is read from the
environment only, is sent only in the `Authorization` header, and is never printed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import yaml

from makna.catalog.base import ConnectorError
from makna.catalog.openmetadata import check_base_url

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "data" / "catalog" / "lending.catalog.yaml"
DEFAULT_URL = "http://localhost:8585"
TOKEN_VARIABLE = "MAKNA_OPENMETADATA_TOKEN"
SERVICE = "lending_sample"
DATABASE = "lending_prod"
GLOSSARY = "lending"
TIMEOUT = 30.0

# normalized type of the common catalog model to an OpenMetadata `dataType`
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


class SeedError(Exception):
    """A call failed or the input is unusable. The message holds a path and a status at most."""


def service_body() -> dict[str, Any]:
    return {
        "name": SERVICE,
        "serviceType": "Postgres",
        "description": "Synthetic lending sample of the Makna development stack.",
    }


def database_body() -> dict[str, Any]:
    return {"name": DATABASE, "service": SERVICE}


def schema_body(schema: str) -> dict[str, Any]:
    return {"name": schema, "database": f"{SERVICE}.{DATABASE}"}


def team_body(name: str) -> dict[str, Any]:
    return {"name": name, "teamType": "Group", "displayName": name}


def user_body(name: str) -> dict[str, Any]:
    return {"name": name, "email": f"{name}@example.invalid"}


def classification_body(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"Classification {name} of the sample catalog."}


def tag_body(tag: str) -> dict[str, Any]:
    classification, _, name = tag.partition(".")
    return {
        "classification": classification,
        "name": name,
        "description": f"Tag {tag} of the sample catalog.",
    }


def glossary_body() -> dict[str, Any]:
    return {"name": GLOSSARY, "description": "Business terms of the lending sample."}


def term_body(term: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "glossary": GLOSSARY,
        "name": term["name"],
        "displayName": term["name"],
        "description": term.get("definition") or term["name"],
        "synonyms": list(term.get("synonyms", [])),
    }


def tag_label(tag: str) -> dict[str, Any]:
    return {"tagFQN": tag, "source": "Classification", "labelType": "Manual", "state": "Confirmed"}


# The export has no length, and the server refuses a VARCHAR column without one
# (DatabaseUtil.validateColumnDataLength, 2.0.7).
DEFAULT_VARCHAR_LENGTH = 255


def data_length(column: Mapping[str, Any]) -> int:
    """The length of `varchar(N)` in the native type, else the default."""
    found = re.fullmatch(r"varchar\((\d+)\)", str(column["native_type"]))
    return int(found.group(1)) if found else DEFAULT_VARCHAR_LENGTH


def column_body(
    column: Mapping[str, Any], position: int, composite: bool = False
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": column["name"],
        "dataType": OM_TYPES[column["data_type"]],
        "dataTypeDisplay": column["native_type"],
    }
    if body["dataType"] == "VARCHAR":
        body["dataLength"] = data_length(column)
    if column.get("description"):
        body["description"] = column["description"]
    body["tags"] = [tag_label(t) for t in column.get("tags", [])]
    if column.get("is_primary_key") and not composite:
        body["constraint"] = "PRIMARY_KEY"
    elif column.get("nullable", True) is False or column.get("is_primary_key"):
        body["constraint"] = "NOT_NULL"
    body["ordinalPosition"] = position + 1
    return body


def table_body(
    dataset: Mapping[str, Any], owner_ids: Mapping[tuple[str, str], str]
) -> dict[str, Any]:
    """The `PUT /api/v1/tables` body; `owner_ids` maps `(type, name)` to the id the server gave."""
    _, schema, name = dataset["engine_fqn"].split(".")
    keys = [c["name"] for c in dataset["columns"] if c.get("is_primary_key")]
    composite = (
        len(keys) > 1
    )  # the server allows one PRIMARY_KEY column; more go in tableConstraints
    body: dict[str, Any] = {"name": name}
    if dataset.get("description"):
        body["description"] = dataset["description"]
    if composite:
        body["tableConstraints"] = [{"constraintType": "PRIMARY_KEY", "columns": keys}]
    body.update(
        {
            "tableType": "Regular",
            "columns": [column_body(c, i, composite) for i, c in enumerate(dataset["columns"])],
            "databaseSchema": f"{SERVICE}.{DATABASE}.{schema}",
            "owners": [
                {"id": owner_ids[(o["type"], o["name"])], "type": o["type"]}
                for o in dataset.get("owners", [])
            ],
            "tags": [tag_label(t) for t in dataset.get("tags", [])],
        }
    )
    return body


def all_tags(catalog: Mapping[str, Any]) -> list[str]:
    found: dict[str, None] = {}
    for dataset in catalog["datasets"]:
        for tag in dataset.get("tags", []):
            found[tag] = None
        for column in dataset["columns"]:
            for tag in column.get("tags", []):
                found[tag] = None
    return list(found)


class Seeder:
    def __init__(self, client: httpx.Client, token: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _send(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> httpx.Response:
        try:
            return self._client.request(method, path, json=body, headers=self._headers)
        except httpx.HTTPError:
            raise SeedError(f"{method} {path}: the server cannot be reached") from None

    def put(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        response = self._send("PUT", path, body)
        if response.status_code not in (200, 201):
            raise SeedError(f"PUT {path}: HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError:
            raise SeedError(f"PUT {path}: the answer is not JSON") from None
        if not isinstance(result, dict):
            raise SeedError(f"PUT {path}: the answer is not an object")
        return result

    def exists(self, path: str) -> bool:
        status = self._send("GET", path).status_code
        if status not in (200, 404):
            raise SeedError(f"GET {path}: HTTP {status}")
        return status == 200

    def ensure_tag(self, tag: str) -> None:
        """Create the classification and the tag only when the server lacks them: a default
        classification such as PII is left as the server shipped it."""
        classification = tag.partition(".")[0]
        if not self.exists(f"/api/v1/classifications/name/{classification}"):
            self.put("/api/v1/classifications", classification_body(classification))
        if not self.exists(f"/api/v1/tags/name/{tag}"):
            self.put("/api/v1/tags", tag_body(tag))

    def owner_id(self, kind: str, name: str) -> str:
        if kind == "team":
            entity = self.put("/api/v1/teams", team_body(name))
        else:
            entity = self.put("/api/v1/users", user_body(name))
        found = entity.get("id")
        if not isinstance(found, str):
            raise SeedError(f"PUT of a {kind}: the answer has no id")
        return found

    def seed(self, catalog: Mapping[str, Any]) -> dict[str, int]:
        self.put("/api/v1/services/databaseServices", service_body())
        self.put("/api/v1/databases", database_body())
        schemas = list(dict.fromkeys(d["engine_fqn"].split(".")[1] for d in catalog["datasets"]))
        for schema in schemas:
            self.put("/api/v1/databaseSchemas", schema_body(schema))
        for tag in all_tags(catalog):
            self.ensure_tag(tag)
        owners = dict.fromkeys(
            (o["type"], o["name"]) for d in catalog["datasets"] for o in d.get("owners", [])
        )
        owner_ids = {key: self.owner_id(*key) for key in owners}
        for dataset in catalog["datasets"]:
            self.put("/api/v1/tables", table_body(dataset, owner_ids))
        self.put("/api/v1/glossaries", glossary_body())
        for term in catalog["glossary_terms"]:
            self.put("/api/v1/glossaryTerms", term_body(term))
        return {
            "tables": len(catalog["datasets"]),
            "glossary_terms": len(catalog["glossary_terms"]),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m deploy.seed_openmetadata",
        description=(
            "Load the sample catalog into OpenMetadata. The token comes from "
            f"{TOKEN_VARIABLE} (a user or bot that may write)."
        ),
    )
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"server base URL (default {DEFAULT_URL})"
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_VARIABLE, "").strip()
    if not token:
        print(f"seed_openmetadata: {TOKEN_VARIABLE} is not set", file=sys.stderr)
        return 2
    try:
        base = check_base_url(args.url)
        catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
        with httpx.Client(base_url=base, timeout=TIMEOUT, follow_redirects=False) as client:
            counts = Seeder(client, token).seed(catalog)
    except (ConnectorError, SeedError, OSError, yaml.YAMLError) as error:
        print(f"seed_openmetadata: {error}", file=sys.stderr)
        return 1
    print(f"seeded {counts['tables']} tables and {counts['glossary_terms']} glossary terms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
