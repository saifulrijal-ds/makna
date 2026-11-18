"""WP-21 (CON-2): the OpenMetadata connector against committed fixtures and a fake transport.

The fixtures were written from the documented shapes of OpenMetadata 2.0.7 and were not recorded
from a live server (`tests/unit/fixtures/openmetadata/README.md`). The cases that need a server
that misbehaves (a timeout, a redirect, an endless cursor) are produced by the fake transport.
"""

from __future__ import annotations

import json
import logging
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from makna.catalog.base import CatalogConnector, ConnectorError
from makna.catalog.file import FileConnector
from makna.catalog.openmetadata import (
    GLOSSARY_TERMS_PATH,
    TABLES_PATH,
    OpenMetadataConnector,
    check_base_url,
)
from makna.model.catalog import CatalogDataset, CatalogGlossaryTerm

from .. import openmetadata_fixtures as generator
from ..catalog_support import EXPORT

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "openmetadata"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl"
BASE = "http://localhost:8585"
SERVICES = {"lending_sample": "lending"}

Handler = Callable[[httpx.Request], httpx.Response]


def load(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def chain(prefix: str) -> dict[str | None, dict[str, Any]]:
    """The generated pages of one listing, keyed by the cursor that asks for them."""
    pages: dict[str | None, dict[str, Any]] = {}
    cursor: str | None = None
    number = 1
    while (FIXTURES / f"{prefix}_page_{number}.json").exists():
        body = load(f"{prefix}_page_{number}.json")
        pages[cursor] = body
        cursor = body["paging"].get("after")
        number += 1
    assert cursor is None
    return pages


class FakeServer:
    """A transport that answers the two listings from pages and records every request."""

    def __init__(
        self,
        tables: dict[str | None, dict[str, Any]] | None = None,
        terms: dict[str | None, dict[str, Any]] | None = None,
    ) -> None:
        self.routes = {TABLES_PATH: tables or {}, GLOSSARY_TERMS_PATH: terms or {}}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"code": 401, "message": "body-canary"})
        pages = self.routes.get(request.url.path)
        if pages is None:
            return httpx.Response(404, json={"message": "body-canary"})
        body = pages.get(request.url.params.get("after"))
        if body is None:
            return httpx.Response(400, json={"message": "body-canary"})
        return httpx.Response(200, json=body)

    def queries(self, path: str) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests if r.url.path == path]


def connector(
    handler: Handler,
    *,
    services: dict[str, str] | None = None,
    page_size: int = 4,
    max_pages: int = 50,
    token: str = TOKEN,
    base_url: str = BASE,
    timeout: float = 30.0,
) -> OpenMetadataConnector:
    return OpenMetadataConnector(
        base_url,
        SecretStr(token),
        services=SERVICES if services is None else services,
        page_size=page_size,
        max_pages=max_pages,
        timeout=timeout,
        transport=httpx.MockTransport(handler),
    )


def sample_server() -> FakeServer:
    return FakeServer(tables=chain("tables"), terms=chain("glossary_terms"))


def single(name: str, path: str = TABLES_PATH) -> FakeServer:
    body: dict[str | None, dict[str, Any]] = {None: load(name)}
    return FakeServer(tables=body) if path == TABLES_PATH else FakeServer(terms=body)


def assert_clean(error: BaseException) -> None:
    """No token and no part of a response body in a message."""
    text = str(error)
    assert TOKEN not in text and "body-canary" not in text, text
    assert text and "\n" not in text


def dump(model: CatalogDataset | CatalogGlossaryTerm) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude={"catalog_id", "updated_at"})


# --- Acceptance 3: the contract shared with the file connector ---


def test_the_connector_satisfies_the_interface() -> None:
    server = sample_server()
    instance: CatalogConnector = connector(server)
    assert instance.kind == "openmetadata" and instance.name == "openmetadata"


def test_the_sample_catalog_reads_equal_through_both_connectors() -> None:
    via_file = FileConnector(EXPORT)
    via_openmetadata = connector(sample_server())
    file_tables = {d.engine_fqn: d for d in via_file.list_datasets()}
    om_tables = {d.engine_fqn: d for d in via_openmetadata.list_datasets()}
    assert len(file_tables) == 9 and set(om_tables) == set(file_tables)
    for fqn, expected in file_tables.items():
        assert dump(om_tables[fqn]) == dump(expected), fqn
        assert [dump_column(c) for c in om_tables[fqn].columns] == [
            dump_column(c) for c in expected.columns
        ]
    file_terms = {t.name: t for t in via_file.list_glossary_terms()}
    om_terms = {t.name: t for t in via_openmetadata.list_glossary_terms()}
    assert len(file_terms) == 8 and set(om_terms) == set(file_terms)
    for name, expected_term in file_terms.items():
        assert dump(om_terms[name]) == dump(expected_term), name


def dump_column(column: Any) -> dict[str, Any]:
    result: dict[str, Any] = column.model_dump(mode="json")
    return result


def test_the_catalog_id_is_the_entity_id_and_the_server_date_is_kept() -> None:
    datasets = list(connector(sample_server()).list_datasets())
    tables = [e for body in chain("tables").values() for e in body["data"]]
    assert [d.catalog_id for d in datasets] == [t["id"] for t in tables]
    assert len({d.catalog_id for d in datasets}) == 9
    assert {d.updated_at.isoformat() for d in datasets if d.updated_at} == {
        "2026-06-30T00:00:00+00:00"
    }
    terms = list(connector(sample_server()).list_glossary_terms())
    assert all(t.catalog_id and "." not in t.catalog_id for t in terms)


def test_the_generated_fixtures_are_what_the_generator_writes() -> None:
    expected = generator.render()
    assert expected and generator.render() == expected
    found = {p.name for p in FIXTURES.glob("*_page_*.json")}
    assert found == set(expected)
    for name, text in expected.items():
        assert (FIXTURES / name).read_bytes() == text.encode("utf-8"), name


# --- Acceptance 1: pagination, tags, owners, synonyms, 401, timeout ---


def test_a_listing_over_several_pages_is_read_in_full_with_the_cursor() -> None:
    server = sample_server()
    datasets = list(connector(server, page_size=4).list_datasets())
    assert len(datasets) == 9
    queries = server.queries(TABLES_PATH)
    assert len(queries) == 3
    assert [q.get("after") for q in queries][0] is None
    assert queries[1]["after"] == chain("tables")[None]["paging"]["after"]
    assert all(q["limit"] == "4" for q in queries)
    assert all(q["fields"] == "columns,tags,owners" for q in queries)
    list(connector(server, page_size=5).list_glossary_terms())
    assert [q["limit"] for q in server.queries(GLOSSARY_TERMS_PATH)] == ["5", "5"]


def test_column_tags_owners_keys_and_types_of_a_table() -> None:
    server = single("hand_tables_owners_and_tags.json")
    (table,) = connector(server).list_datasets()
    assert table.engine_fqn == "lending.core.loan_review_notes"
    assert table.catalog_id == "0b7f1c52-8a31-4a0e-9d52-3c1d5e7a9001"
    assert [(o.name, o.type, o.email) for o in table.owners] == [
        ("rina.wijaya", "user", None),
        ("data-governance", "team", None),
    ]
    assert table.tags == ["Tier.Tier2"]  # duplicate once, the suggested label left out
    assert table.updated_at is not None and table.updated_at.isoformat() == (
        "2026-07-01T00:00:00+00:00"
    )
    by_name = {c.name: c for c in table.columns}
    assert by_name["reviewer_name"].tags == ["PII.Sensitive", "lending.Reviewer"]
    assert by_name["reviewer_name"].description == ""
    assert [c.ordinal for c in table.columns] == [0, 1, 2, 3, 4, 5]
    # A composite key comes from tableConstraints; NOT_NULL is not a key.
    assert [(c.name, c.is_primary_key, c.nullable) for c in table.columns[:3]] == [
        ("note_id", True, False),
        ("contract_id", True, False),
        ("reviewer_name", False, True),
    ]
    assert [c.data_type for c in table.columns] == [
        "integer", "integer", "string", "other", "float", "timestamp",
    ]  # fmt: skip
    assert by_name["score"].native_type == "double precision"


def test_the_sample_columns_with_tags_keep_them() -> None:
    customers = next(d for d in connector(sample_server()).list_datasets() if d.name == "customers")
    tagged = {c.name for c in customers.columns if c.tags}
    assert tagged == {"full_name", "national_id", "phone_number", "email", "birth_date"}
    assert all(c.tags == ["PII.Sensitive"] for c in customers.columns if c.tags)


def test_a_glossary_term_keeps_its_synonyms() -> None:
    server = single("hand_glossary_term_with_synonyms.json", GLOSSARY_TERMS_PATH)
    (term,) = connector(server).list_glossary_terms()
    assert term.name == "Write-off" and term.catalog_id == "7c4d9e10-0000-4000-8000-000000000001"
    assert term.definition.startswith("Removal of an uncollectible")
    assert term.synonyms == ["charge-off", "hapus buku"]
    assert term.related_terms == ["NPF"] and term.tags == ["PII.NonSensitive"]
    assert server.queries(GLOSSARY_TERMS_PATH)[0]["fields"] == "relatedTerms,tags"


def test_a_401_is_a_clear_error_without_the_body() -> None:
    server = sample_server()
    wrong = connector(server, token="another-token")
    for call in (wrong.check, lambda: list(wrong.list_datasets())):
        with pytest.raises(ConnectorError) as raised:
            call()
        assert_clean(raised.value)
        message = str(raised.value)
        assert TABLES_PATH in message and "401" in message and "MAKNA_OPENMETADATA_TOKEN" in message
        assert "another-token" not in message


def test_the_recorded_401_body_is_not_quoted() -> None:
    body = (FIXTURES / "hand_error_401.json").read_text(encoding="utf-8")
    instance = connector(lambda request: httpx.Response(401, content=body))
    with pytest.raises(ConnectorError) as raised:
        list(instance.list_datasets())
    assert "body-canary" in body and "body-canary" not in str(raised.value)
    assert "person@example" not in str(raised.value)


def test_another_status_names_the_path_and_the_status_only() -> None:
    body = (FIXTURES / "hand_error_500.json").read_text(encoding="utf-8")
    instance = connector(lambda request: httpx.Response(500, content=body))
    with pytest.raises(ConnectorError) as raised:
        list(instance.list_glossary_terms())
    assert str(raised.value) == f"OpenMetadata answered {GLOSSARY_TERMS_PATH} with HTTP 500"


def test_a_timeout_is_a_clear_error() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out for Bearer " + TOKEN, request=request)

    instance = connector(slow, timeout=2.5)
    with pytest.raises(ConnectorError) as raised:
        instance.check()
    assert_clean(raised.value)
    assert str(raised.value) == f"OpenMetadata did not answer {TABLES_PATH} within 2.5 seconds"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_a_connection_failure_is_a_clear_error() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused: Bearer " + TOKEN, request=request)

    with pytest.raises(ConnectorError) as raised:
        connector(refused).check()
    assert_clean(raised.value)
    assert "cannot be reached" in str(raised.value)


# --- The rulings ---


def test_a_table_of_an_unmapped_service_is_skipped_and_counted() -> None:
    server = single("hand_tables_unmapped_service.json")
    instance = connector(server)
    datasets = list(instance.list_datasets())
    assert [d.engine_fqn for d in datasets] == ["lending.core.branches_copy"]
    assert instance.skipped_services == {"marketing_sample": 2}
    mapped = connector(server, services={"lending_sample": "lending", "marketing_sample": "web"})
    assert [d.engine_fqn for d in mapped.list_datasets()] == [
        "web.public.web_sessions",
        "web.public.campaigns",
        "lending.core.branches_copy",
    ]
    assert mapped.skipped_services == {}


def test_two_tables_that_map_to_one_engine_name_are_an_error() -> None:
    body = load("hand_tables_unmapped_service.json")
    twin = json.loads(json.dumps(body["data"][2]))
    twin["id"] = "0b7f1c52-8a31-4a0e-9d52-3c1d5e7a9199"
    twin["database"] = {"id": "x", "type": "database", "name": "other_database"}
    body["data"].append(twin)
    with pytest.raises(ConnectorError, match="map to the same engine_fqn") as raised:
        list(connector(FakeServer(tables={None: body})).list_datasets())
    assert "lending_sample" in str(raised.value)


def test_a_repeated_cursor_stops_the_listing() -> None:
    pages = chain("tables")
    first_cursor = pages[None]["paging"]["after"]
    looping = {None: pages[None], first_cursor: {"data": [], "paging": {"after": first_cursor}}}
    server = FakeServer(tables=looping)
    with pytest.raises(ConnectorError, match="repeated a page cursor") as raised:
        list(connector(server).list_datasets())
    assert TABLES_PATH in str(raised.value)
    assert len(server.requests) == 2


def test_the_page_cap_stops_a_listing_that_never_ends() -> None:
    def endless(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params.get("after", "0")) + 1
        return httpx.Response(200, json={"data": [], "paging": {"after": str(number)}})

    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return endless(request)

    with pytest.raises(ConnectorError, match="more than 3 pages"):
        list(connector(recording, max_pages=3).list_datasets())
    assert len(seen) == 3


def test_a_listing_of_exactly_the_cap_is_accepted() -> None:
    server = sample_server()
    assert len(list(connector(server, max_pages=3).list_datasets())) == 9
    with pytest.raises(ConnectorError, match="more than 2 pages"):
        list(connector(server, max_pages=2).list_datasets())


@pytest.mark.parametrize(
    "url",
    [
        "http://catalog.example.org",
        "http://192.0.2.10:8585",
        "http://localhost.example.org",
        "ftp://localhost",
        "localhost:8585",
        "",
        "https://",
        "https://user:pw@catalog.example.org",
        "https://catalog.example.org?x=1",
        "https://catalog.example.org:notaport",
    ],
)
def test_a_base_url_that_could_send_the_token_in_clear_is_refused(url: str) -> None:
    with pytest.raises(ConnectorError) as raised:
        connector(sample_server(), base_url=url)
    assert_clean(raised.value)
    assert "catalog.example.org" not in str(raised.value) and "pw@" not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://catalog.example.org",
        "https://catalog.example.org:8443/om/",
        "http://localhost:8585",
        "http://127.0.0.1:8585/",
        "HTTP://LOCALHOST",
    ],
)
def test_https_and_the_local_http_urls_are_accepted(url: str) -> None:
    assert check_base_url(url) == url.strip().rstrip("/")


def test_a_redirect_is_never_followed() -> None:
    seen: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://elsewhere.example.org/api"})

    with pytest.raises(ConnectorError) as raised:
        list(connector(redirect).list_datasets())
    assert str(raised.value) == f"OpenMetadata answered {TABLES_PATH} with HTTP 302"
    assert [r.url.host for r in seen] == ["localhost"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>body-canary</html>"),
        httpx.Response(200, json=["body-canary"]),
        httpx.Response(200, json={"data": "body-canary"}),
        httpx.Response(200, json={"data": [{"id": "1"}]}),  # a table without a name
        httpx.Response(200, json={"data": [{"id": "1", "name": "t", "columns": "body-canary"}]}),
        httpx.Response(
            200, json={"data": [{"id": "1", "name": "t", "columns": [{"name": "c"}]}]}
        ),  # a column without a type
    ],
)
def test_an_answer_of_the_wrong_shape_is_an_error_without_the_body(
    response: httpx.Response,
) -> None:
    with pytest.raises(ConnectorError) as raised:
        list(connector(lambda request: response).list_datasets())
    assert_clean(raised.value)
    assert "Makna cannot read" in str(raised.value) or "not JSON" in str(raised.value)


def test_a_table_without_a_service_or_a_four_part_name_is_a_shape_error() -> None:
    body = {"data": [{"id": "1", "name": "t", "columns": [], "fullyQualifiedName": "a.b"}]}
    with pytest.raises(ConnectorError, match="Makna cannot read"):
        list(connector(lambda request: httpx.Response(200, json=body)).list_datasets())
    body["data"][0]["fullyQualifiedName"] = "lending_sample.db.core.t"
    (dataset,) = connector(lambda request: httpx.Response(200, json=body)).list_datasets()
    assert dataset.engine_fqn == "lending.core.t"


def test_a_table_name_the_model_cannot_hold_is_an_error() -> None:
    body = load("hand_tables_unmapped_service.json")
    body["data"][2]["name"] = "a.b"
    with pytest.raises(ConnectorError, match="cannot be mapped"):
        list(connector(FakeServer(tables={None: body})).list_datasets())


# --- The token ---


def test_the_token_is_in_no_repr_no_message_and_no_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    server = sample_server()
    instance = connector(server)
    instance.check()
    assert (
        len(list(instance.list_datasets())) == 9 and len(list(instance.list_glossary_terms())) == 8
    )
    assert {r.headers["authorization"] for r in server.requests} == {f"Bearer {TOKEN}"}
    assert TOKEN not in repr(instance) and TOKEN not in str(vars(instance).get("_token"))
    assert TOKEN not in caplog.text and TOKEN not in repr(SecretStr(TOKEN))
    failing = connector(lambda request: httpx.Response(401, json={"message": TOKEN}))
    with pytest.raises(ConnectorError) as raised:
        failing.check()
    assert TOKEN not in str(raised.value) and TOKEN not in caplog.text


@pytest.mark.parametrize("token", ["", "two words", "line\nbreak", "tökén", "a" * 3 + "\r"])
def test_a_token_with_a_character_no_token_has_is_refused_without_showing_it(token: str) -> None:
    with pytest.raises(ConnectorError) as raised:
        connector(sample_server(), token=token)
    assert token.strip() not in str(raised.value) or not token.strip()
    assert "character a token never has" in str(raised.value)


# --- Acceptance 2: no OpenMetadata SDK ---


def test_the_lock_file_has_no_openmetadata_package() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    names = {package["name"].lower() for package in lock["package"]}
    assert names, "the lock file lists no packages"
    assert not {n for n in names if n.startswith("openmetadata")}
    assert "makna-sl" in names


def test_no_module_imports_the_openmetadata_sdk() -> None:
    # The SDK's import package is `metadata`; nothing of Makna may import it.
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src").rglob("*.py")
        if any(
            line.startswith(("import metadata", "from metadata"))
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert offenders == []
