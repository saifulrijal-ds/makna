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
    assert set(images) == {"mysql", "elasticsearch", "migrate", "server"}
    for image in images.values():
        tag = image.rsplit(":", 1)[1]
        assert tag.count(".") == 2 and tag[0].isdigit(), image
        assert "latest" not in image
    assert (
        images["server"] == images["migrate"] == ("docker.getcollate.io/openmetadata/server:2.0.7")
    )
    for image in set(images.values()):
        assert image in readme, image


def test_the_stack_binds_to_localhost_and_is_separate_from_the_main_stack() -> None:
    for service in COMPOSE["services"].values():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), port
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
        assert set(body) <= {
            "name", "description", "tableType", "columns", "databaseSchema", "owners", "tags",
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
