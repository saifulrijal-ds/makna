"""WP-21 (CON-2): the OpenMetadata compose file and the seeding script, checked without a server.

Neither can be run in the session that wrote them (no Docker, no server). These tests keep the
compose file pinned and recorded in the README, and check the bodies the seeder would send
against the shapes of the connector's fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from deploy import seed_openmetadata as seed

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
FIXTURES = Path(__file__).parent / "fixtures" / "openmetadata"
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.openmetadata.yml").read_text(encoding="utf-8"))
CATALOG = yaml.safe_load(seed.DEFAULT_CATALOG.read_text(encoding="utf-8"))


def test_every_image_is_pinned_to_an_exact_version_and_the_readme_records_them() -> None:
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    images = {name: s["image"] for name, s in COMPOSE["services"].items()}
    assert set(images) == {"postgresql", "opensearch", "migrate", "server"}
    for image in images.values():
        tag = image.rsplit(":", 1)[1]
        assert tag.count(".") == 2 and tag[0].isdigit(), image
        assert "latest" not in image
    assert (
        images["server"] == images["migrate"] == ("docker.getcollate.io/openmetadata/server:2.0.7")
    )
    for image in set(images.values()):
        assert image in readme, image
    # Elasticsearch and MySQL are not permissive: OpenSearch (Apache-2.0) and PostgreSQL.
    assert "elastic" not in "".join(images.values()) and "mysql" not in "".join(images.values())
    assert images["opensearch"] == "opensearchproject/opensearch:3.4.0"
    licences = {
        "docker.getcollate.io/openmetadata/postgresql:2.0.7": "PostgreSQL License",
        "opensearchproject/opensearch:3.4.0": "Apache-2.0",
        "docker.getcollate.io/openmetadata/server:2.0.7": "Apache-2.0",
    }
    for image, licence in licences.items():
        row = next(
            line for line in readme.splitlines() if line.startswith("|") and f"`{image}`" in line
        )
        assert licence in row, image
    assert set(licences) == set(images.values())
    environment = COMPOSE["x-server-environment"]
    assert environment["SEARCH_TYPE"] == "opensearch"
    assert environment["ELASTICSEARCH_HOST"] == "opensearch"


def test_the_stack_binds_to_localhost_and_is_separate_from_the_main_stack() -> None:
    published = {name: service.get("ports", []) for name, service in COMPOSE["services"].items()}
    assert published == {
        "postgresql": [],
        "opensearch": [],
        "migrate": [],
        "server": ["127.0.0.1:8585:8585"],
    }
    main = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    assert COMPOSE["name"] != main.get("name")
    assert not set(COMPOSE["services"]) & set(main["services"])


def fixture_tables() -> dict[str, dict[str, Any]]:
    tables = {}
    for path in sorted(FIXTURES.glob("tables_page_*.json")):
        for table in json.loads(path.read_text(encoding="utf-8"))["data"]:
            tables[table["name"]] = table
    return tables


def test_the_table_bodies_match_the_shapes_of_the_fixtures() -> None:
    owners = {("team", "data-governance"): "11111111-1111-4111-8111-111111111111"}
    fixtures = fixture_tables()
    assert len(CATALOG["datasets"]) == len(fixtures) == 9
    for dataset in CATALOG["datasets"]:
        body = seed.table_body(dataset, owners)
        shape = fixtures[dataset["name"]]
        assert body["name"] == shape["name"]
        assert body.get("description") == shape.get("description")
        assert body["tableType"] == shape["tableType"]
        assert body["tags"] == shape["tags"]
        assert body["databaseSchema"] == shape["databaseSchema"]["fullyQualifiedName"]
        assert body["columns"] == [
            {k: v for k, v in column.items() if k != "fullyQualifiedName"}
            for column in shape["columns"]
        ]
        assert [(o["type"]) for o in body["owners"]] == [o["type"] for o in shape["owners"]]
        assert {o["id"] for o in body["owners"]} == set(owners.values())
        assert body.get("tableConstraints") == shape.get("tableConstraints")
        assert set(body) <= {
            "name", "description", "tableType", "columns", "databaseSchema", "owners", "tags",
            "tableConstraints",
        }  # fmt: skip


def test_the_service_names_are_the_ones_of_the_fixtures() -> None:
    table = next(iter(fixture_tables().values()))
    assert table["service"]["name"] == seed.SERVICE
    assert table["database"]["name"] == seed.DATABASE
    assert seed.database_body() == {"name": seed.DATABASE, "service": seed.SERVICE}


def test_glossary_term_bodies_keep_the_synonyms_of_the_fixtures() -> None:
    shapes = {}
    for path in sorted(FIXTURES.glob("glossary_terms_page_*.json")):
        for term in json.loads(path.read_text(encoding="utf-8"))["data"]:
            shapes[term["name"]] = term
    assert len(shapes) == len(CATALOG["glossary_terms"]) == 8
    for term in CATALOG["glossary_terms"]:
        body = seed.term_body(term)
        shape = shapes[term["name"]]
        assert body["synonyms"] == shape["synonyms"] and body["description"] == shape["description"]
        assert body["glossary"] == seed.GLOSSARY == shape["glossary"]["name"]


def test_the_catalog_tags_are_the_ones_the_seeder_ensures() -> None:
    assert seed.all_tags(CATALOG) == ["PII.Sensitive"]
    assert seed.tag_body("PII.Sensitive")["classification"] == "PII"


def test_the_seeder_only_puts_and_a_second_run_sends_the_same_requests() -> None:
    def run() -> list[tuple[str, str, str]]:
        sent: list[tuple[str, str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append((request.method, request.url.path, request.read().decode()))
            assert request.headers["authorization"] == "Bearer test-token"
            if request.method == "GET":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"id": "22222222-2222-4222-8222-222222222222"})

        client = httpx.Client(
            base_url="http://localhost:8585", transport=httpx.MockTransport(handler)
        )
        counts = seed.Seeder(client, "test-token").seed(CATALOG)
        assert counts == {"tables": 9, "glossary_terms": 8}
        return sent

    first, second = run(), run()
    assert first == second
    assert {m for m, _, _ in first} <= {"PUT", "GET"}
    assert [p for m, p, _ in first if m == "PUT"].count("/api/v1/tables") == 9
    puts = [p for m, p, _ in first if m == "PUT"]
    assert puts.index("/api/v1/teams") < puts.index("/api/v1/tables")


def test_a_failed_call_names_the_path_and_status_only() -> None:
    client = httpx.Client(
        base_url="http://localhost:8585",
        transport=httpx.MockTransport(lambda r: httpx.Response(403, text="body-canary")),
    )
    with pytest.raises(seed.SeedError) as raised:
        seed.Seeder(client, "test-token").seed(CATALOG)
    assert str(raised.value) == "PUT /api/v1/services/databaseServices: HTTP 403"


def test_the_seeder_needs_the_token_and_a_safe_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MAKNA_OPENMETADATA_TOKEN", raising=False)
    assert seed.main([]) == 2
    assert "MAKNA_OPENMETADATA_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("MAKNA_OPENMETADATA_TOKEN", "secret-token-value")
    assert seed.main(["--url", "http://catalog.example.org"]) == 1
    err = capsys.readouterr().err
    assert "https://" in err and "secret-token-value" not in err


# --- The create bodies against what OpenMetadata 2.0.7 requires on create ---

# `required` of the create schemas at the tag (api/data, api/services, api/teams,
# api/classification,
# api/data/createGlossary*), and the rules of `DatabaseUtil.validateColumns` and
# `validateConstraints` (openmetadata-service, resources/databases), which `PUT /api/v1/tables` runs
# on every body. The create schemas also have `additionalProperties: false`.
REQUIRED = {
    "service": {"name", "serviceType"},
    "database": {"name", "service"},
    "schema": {"name", "database"},
    "team": {"name", "teamType"},
    "user": {"name", "email"},
    "classification": {"name", "description"},
    "tag": {"name", "description"},
    "glossary": {"name", "description"},
    "term": {"glossary", "name", "description"},
    "table": {"name", "columns", "databaseSchema"},
}
LENGTH_TYPES = {"CHAR", "VARCHAR", "BINARY", "VARBINARY"}


def table_problems(body: dict[str, Any]) -> list[str]:
    """The reasons the server would answer 400 to a table body (rules of DatabaseUtil)."""
    problems = []
    columns = body["columns"]
    names = [c["name"] for c in columns]
    if len(set(names)) != len(names):
        problems.append("a column name is repeated")
    for column in columns:
        if not {"name", "dataType"} <= set(column):
            problems.append(f"{column.get('name')}: name and dataType are required")
        if column["dataType"] in LENGTH_TYPES and not column.get("dataLength", 0) > 0:
            problems.append(f"{column['name']}: dataLength must not be null")
        if column["dataType"] == "ARRAY" and "arrayDataType" not in column:
            problems.append(f"{column['name']}: arrayDataType must not be null")
        if column.get("scale") is not None and column.get("precision") is None:
            problems.append(f"{column['name']}: scale without precision")
    key_columns = [c for c in columns if c.get("constraint") == "PRIMARY_KEY"]
    if len(key_columns) > 1:
        problems.append("multiple columns tagged with primary key constraints")
    for constraint in body.get("tableConstraints", []):
        if constraint["constraintType"] == "PRIMARY_KEY" and key_columns:
            problems.append("a column key and a table key together")
        if not set(constraint["columns"]) <= set(names):
            problems.append("a table constraint names a column that does not exist")
    return problems


def test_every_create_body_satisfies_what_the_server_requires() -> None:
    owners = {("team", "data-governance"): "11111111-1111-4111-8111-111111111111"}
    for dataset in CATALOG["datasets"]:
        body = seed.table_body(dataset, owners)
        assert REQUIRED["table"] <= set(body)
        assert table_problems(body) == [], dataset["name"]
    assert REQUIRED["service"] <= set(seed.service_body())
    assert REQUIRED["database"] <= set(seed.database_body())
    assert REQUIRED["schema"] <= set(seed.schema_body("core"))
    assert REQUIRED["team"] <= set(seed.team_body("data-governance"))
    assert REQUIRED["user"] <= set(seed.user_body("rina.wijaya"))
    assert REQUIRED["classification"] <= set(seed.classification_body("PII"))
    assert REQUIRED["tag"] <= set(seed.tag_body("PII.Sensitive"))
    assert REQUIRED["glossary"] <= set(seed.glossary_body())
    for term in CATALOG["glossary_terms"]:
        assert REQUIRED["term"] <= set(seed.term_body(term))
        assert seed.term_body(term)["description"]


def test_the_sample_has_a_varchar_a_composite_key_and_the_checker_catches_both() -> None:
    owners = {("team", "data-governance"): "11111111-1111-4111-8111-111111111111"}
    bodies = {d["name"]: seed.table_body(d, owners) for d in CATALOG["datasets"]}
    snapshots = bodies["contract_monthly_snapshots"]
    assert snapshots["tableConstraints"] == [
        {"constraintType": "PRIMARY_KEY", "columns": ["snapshot_date", "contract_id"]}
    ]
    assert not [c for c in snapshots["columns"] if c.get("constraint") == "PRIMARY_KEY"]
    assert any(c["dataType"] == "VARCHAR" for b in bodies.values() for c in b["columns"])
    broken = json.loads(json.dumps(bodies["customers"]))
    for column in broken["columns"]:
        column.pop("dataLength", None)
    assert any("dataLength" in p for p in table_problems(broken))
    keys = json.loads(json.dumps(bodies["branches"]))
    keys["columns"][1]["constraint"] = "PRIMARY_KEY"
    assert any("multiple columns" in p for p in table_problems(keys))
