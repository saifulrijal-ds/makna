"""WP-17 acceptance 1 to 5 and 7 (WEB-1), and the rulings on visibility, escaping, names in paths,
and the search partial (integration tier, on the real application).

The module shares one indexed database and only reads it. A test that changes the definitions
uses its own database (`fresh_service_world`).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import yaml
from sqlalchemy import Engine

from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer
from makna.model.governance import STRICT
from makna.search.embedding import HashProvider
from makna.services.derive import make_derive

from ..catalog_support import DEFINITIONS
from . import web_support as w

LENDING = DEFINITIONS / "lending"
SCRIPT = '<script>alert("x")</script>'


@pytest.fixture(scope="module")
def world(service_world: tuple[Engine, Any]) -> Iterator[tuple[Engine, Any]]:
    engine, _ = service_world
    app = w.make_app(engine.url)
    yield engine, app
    app.state.services.dispose()


def client_for(world: tuple[Engine, Any], role: str) -> Any:
    engine, app = world
    return w.signed_in(app, w.make_user(engine, role))


class Links(HTMLParser):
    """The `href` of every link, decoded, and the text of the page's `<script>` elements."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.scripts: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.hrefs.append(str(values["href"]))
        if tag == "script":
            self.scripts.append(values)


def parsed(html: str) -> Links:
    parser = Links()
    parser.feed(html)
    return parser


def fields(html: str) -> list[str]:
    """The `data-field` marks of a page, in the order they appear."""
    return re.findall(r'data-field="([a-z\-]+)"', html)


def section(html: str, field: str) -> str:
    start = html.index(f'data-field="{field}"')
    return html[start : start + 3000]


def items(html: str) -> list[str]:
    """The list items of the results or objects lists, not those of the navigation."""
    lists = re.findall(r'<(?:ol|ul) class="(?:results|objects)">(.*?)</(?:ol|ul)>', html, re.S)
    return [item for body in lists for item in re.findall(r"<li>(.*?)</li>", body, re.S)]


def reindex(engine: Engine, root: Path) -> None:
    objects = definition_objects(load_definitions(root))
    indexer.build(engine, HashProvider(), objects, derive=make_derive(STRICT))


# --- criterion 1: the metric page ---


def test_the_metric_page_shows_its_parts_in_the_order_of_the_plan(world: Any) -> None:
    html = client_for(world, "viewer").get("/metrics/npf_ratio").text
    marks = fields(html)
    wanted = [
        "label",
        "description",
        "formula",
        "owner",
        "status",
        "freshness",
        "caveats",
        "example-questions",
    ]
    assert [m for m in marks if m in wanted] == wanted
    assert "NPF Ratio" in section(html, "label")
    assert "Outstanding principal" in section(html, "formula") or "NPF" in section(html, "formula")
    assert "credit-risk" in section(html, "owner")
    status = section(html, "status")
    assert "approved" in status and "version 1" in status
    assert "freshness not available" in section(html, "freshness")


def test_a_viewer_sees_no_sql_and_no_yaml_on_any_discovery_page(world: Any) -> None:
    client = client_for(world, "viewer")
    pages = [
        "/",
        "/search?q=npf",
        "/domains/lending",
        "/metrics/npf_ratio",
        "/metrics/outstanding_principal",
        "/entities/contract_snapshot",
        "/glossary/npf",
    ]
    for path in pages:
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.text
        assert "<pre" not in html and "<textarea" not in html, path
        for text in ("SELECT ", "FROM ", "GROUP BY", "apiVersion", "numerator:", "kind: metric"):
            assert text not in html, (path, text)
        assert "example_requests" not in html and "join_paths" not in html, path


# --- criterion 2: search and the partial ---


def test_searching_kredit_macet_lists_the_npf_ratio_in_the_first_three_with_its_badge(
    world: Any,
) -> None:
    html = client_for(world, "viewer").get("/search", params={"q": "kredit macet"}).text
    first = items(html)[:3]
    assert any('href="/metrics/npf_ratio"' in item and "NPF Ratio" in item for item in first)
    match = next(item for item in first if 'href="/metrics/npf_ratio"' in item)
    assert "badge-approved" in match and ">approved<" in match


def test_the_htmx_request_gets_only_the_results_fragment(world: Any) -> None:
    client = client_for(world, "viewer")
    full = client.get("/search", params={"q": "kredit macet"})
    partial = client.get("/search", params={"q": "kredit macet"}, headers={"HX-Request": "true"})
    assert full.status_code == partial.status_code == 200
    assert "<html" in full.text and "<nav" in full.text
    assert "<html" not in partial.text and "<nav" not in partial.text
    assert 'href="/metrics/npf_ratio"' in partial.text
    # The fragment is exactly the part of the page that the results fill.
    assert partial.text.strip() in full.text
    assert full.headers["vary"] == partial.headers["vary"] == "HX-Request"
    # Without a question there is no search and nothing to show.
    assert items(client.get("/search", headers={"HX-Request": "true"}).text) == []


def test_the_search_box_asks_for_the_fragment_as_you_type_without_inline_script(
    world: Any,
) -> None:
    client = client_for(world, "viewer")
    for path in ("/", "/search?q=npf", "/metrics/npf_ratio"):
        response = client.get(path)
        scripts = parsed(response.text).scripts
        assert scripts and all(s.get("src") for s in scripts), path  # no inline script
        policy = response.headers["content-security-policy"]
        assert "'unsafe-inline'" not in policy.split("style-src")[0]
        assert "unsafe-eval" not in policy
    html = client.get("/search").text
    assert 'hx-get="/search"' in html and "delay:" in html and 'hx-target="#search-results"' in html


def test_a_search_text_that_cannot_be_used_is_a_400_with_fixed_text(world: Any) -> None:
    client = client_for(world, "viewer")
    long = client.get("/search", params={"q": "a" * 501})
    assert long.status_code == 400 and "cannot be used" in long.text
    assert "a" * 50 not in long.text  # the text that was refused is not echoed
    control = client.get("/search", params={"q": "bad\x00text"})
    assert control.status_code == 400 and "cannot be used" in control.text
    assert "bad" not in control.text


# --- criterion 3: drafts ---


def test_a_viewer_does_not_see_a_draft_in_search_or_on_the_domain_page(world: Any) -> None:
    client = client_for(world, "viewer")
    assert "write_off_amount" not in client.get("/search", params={"q": "write off"}).text
    assert "Write-Off" not in client.get("/search", params={"q": "write off"}).text
    domain = client.get("/domains/lending").text
    assert "Write-Off" not in domain and "write_off_amount" not in domain


def test_a_curator_sees_the_draft_with_a_draft_badge(world: Any) -> None:
    client = client_for(world, "curator")
    for path in ("/search?q=write+off+amount", "/domains/lending"):
        html = client.get(path).text
        item = next(i for i in items(html) if 'href="/metrics/write_off_amount"' in i)
        assert "badge-draft" in item and ">draft<" in item, path
    page = client.get("/metrics/write_off_amount")
    assert page.status_code == 200 and ">draft<" in section(page.text, "status")


def test_a_draft_name_is_the_same_404_as_an_unknown_name_for_a_viewer(world: Any) -> None:
    client = client_for(world, "viewer")
    draft = client.get("/metrics/write_off_amount")
    unknown = client.get("/metrics/no_such_metric")
    assert draft.status_code == unknown.status_code == 404
    assert draft.text == unknown.text
    assert "Write-Off" not in draft.text and "write_off_amount" not in draft.text
    # A viewer's glossary or entity page of a name that is another kind is a 404 too.
    assert client.get("/entities/npf_ratio").status_code == 404
    assert client.get("/glossary/contract_snapshot").status_code == 404
    assert client.get("/domains/no_such_domain").status_code == 404


def test_an_agent_token_user_s_session_still_sees_only_what_the_role_allows(world: Any) -> None:
    # A signed-in session is a user, not a token: an approver sees drafts like a curator.
    html = client_for(world, "approver").get("/metrics/write_off_amount")
    assert html.status_code == 200


# --- criterion 5: the domain page ---


def test_the_domain_page_lists_every_approved_object_grouped_by_kind(world: Any) -> None:
    html = client_for(world, "viewer").get("/domains/lending").text
    groups = dict(
        re.findall(r'<section data-group="([^"]+)">(.*?)</section>', html, re.S),
    )
    assert list(groups) == ["Metrics", "Entities", "Glossary terms"]
    expected: dict[str, set[str]] = {"metric": set(), "entity": set(), "glossary_term": set()}
    for path in sorted(LENDING.rglob("*.yaml")):
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
        if content["kind"] in expected and content["status"] == "approved":
            expected[content["kind"]].add(content["name"])
    assert len(expected["metric"]) >= 20 and expected["entity"] and expected["glossary_term"]
    for kind, title in (
        ("metric", "Metrics"),
        ("entity", "Entities"),
        ("glossary_term", "Glossary terms"),
    ):
        prefix = {"metric": "metrics", "entity": "entities", "glossary_term": "glossary"}[kind]
        listed = set(re.findall(rf'href="/{prefix}/([^"]+)"', groups[title]))
        assert listed == expected[kind], kind


def test_the_start_page_lists_the_domains_and_recent_metrics_and_has_a_search_box(
    world: Any,
) -> None:
    html = client_for(world, "viewer").get("/").text
    assert 'action="/search"' in html and 'name="q"' in html
    assert 'href="/domains/lending"' in html and "Consumer Lending" in html
    recent = section(html, "recent")
    assert len(re.findall(r'href="/metrics/', recent)) == 5
    assert "write_off_amount" not in html


# --- the other pages ---


def test_the_entity_and_glossary_pages_show_their_parts_and_link_on(world: Any) -> None:
    client = client_for(world, "viewer")
    entity = client.get("/entities/contract").text
    assert "Joins to" in entity and 'href="/entities/customer"' in entity
    assert "credit-risk" in section(entity, "owner")
    term = client.get("/glossary/npf").text
    assert 'href="/metrics/npf_ratio"' in term
    assert "kredit macet" in term  # the synonyms are shown
    metric = client.get("/metrics/npf_ratio").text
    assert 'href="/glossary/npf"' in metric  # the related objects of a metric


# --- names in paths ---


@pytest.mark.parametrize(
    "path",
    [
        "/metrics/..%2Fetc%2Fpasswd",
        "/metrics/a%20b",
        "/metrics/npf_ratio%0A",
        "/metrics/%E2%82%AC",
        "/metrics/-leading-dash",
        "/metrics/" + "x" * 300,
        "/entities/%2e%2e",
        "/glossary/npf%3Bdrop",
        "/domains/lending%00",
        "/metrics/metric:npf_ratio",
    ],
)
def test_a_name_that_is_not_an_object_name_is_the_fixed_404(world: Any, path: str) -> None:
    client = client_for(world, "admin")
    response = client.get(path)
    assert response.status_code == 404
    assert "This page does not exist." in response.text
    assert "npf_ratio%0A" not in response.text and "etc" not in response.text


# --- criterion 7 and escaping ---


def test_every_example_question_links_to_the_bench_with_the_question_encoded(world: Any) -> None:
    html = client_for(world, "viewer").get("/metrics/npf_ratio").text
    questions = yaml.safe_load((LENDING / "metrics" / "npf_ratio.yaml").read_text())[
        "example_questions"
    ]
    assert questions
    links = [h for h in parsed(section(html, "example-questions")).hrefs if h.startswith("/bench")]
    assert links == ["/bench?" + urlencode({"q": q}) for q in questions]
    for question, href in zip(questions, links, strict=True):
        assert parse_qs(urlsplit(href).query) == {"q": [question]}


def test_stored_text_is_escaped_and_a_question_with_special_characters_is_encoded(
    fresh_service_world: tuple[Engine, Any], tmp_path: Path
) -> None:
    engine, users = fresh_service_world
    root = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, root)
    path = root / "lending" / "metrics" / "penalty_income.yaml"
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    question = 'Fees & "penalties" per <branch> & region?'
    content["description"] = f"{SCRIPT} " + content["description"]
    content["caveats"] = f"{SCRIPT} careful"
    content["synonyms"]["en"].append(SCRIPT)
    content["example_questions"] = [question, "A second question, with a comma?"]
    path.write_text(yaml.safe_dump(content, sort_keys=False, allow_unicode=True), encoding="utf-8")
    reindex(engine, root)
    app = w.make_app(engine.url)
    try:
        client = w.signed_in(app, w.make_user(engine, "viewer"))
        pages = [
            client.get("/metrics/penalty_income"),
            client.get("/search", params={"q": "penalty income"}),
            client.get("/domains/lending"),
            client.get("/search", params={"q": SCRIPT}),
        ]
        for response in pages:
            assert response.status_code == 200
            assert "<script>alert" not in response.text
            assert all(s.get("src") for s in parsed(response.text).scripts)
        page = pages[0].text
        assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in page
        # The question in the link is URL-encoded, then escaped for HTML; parsed back it is exact.
        links = [h for h in parsed(page).hrefs if h.startswith("/bench")]
        assert links[0] == "/bench?" + urlencode({"q": question})
        assert parse_qs(urlsplit(links[0]).query)["q"] == [question]
        assert "&amp;" in page and "&#34;penalties&#34;" in page  # the visible text, escaped
        # What was typed into the search box comes back escaped inside the attribute.
        assert 'value="&lt;script&gt;' in pages[3].text
    finally:
        app.state.services.dispose()


def test_stored_text_is_escaped_on_the_entity_glossary_and_domain_pages_and_in_the_owner(
    fresh_service_world: tuple[Engine, Any], tmp_path: Path
) -> None:
    engine, _ = fresh_service_world
    root = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, root)
    planted = {
        "domain.yaml": None,
        "entities/contract.yaml": None,
        "glossary/npf.yaml": None,
        "metrics/penalty_income.yaml": None,
    }
    for name in planted:
        path = root / "lending" / name
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
        content["description"] = f"{SCRIPT} " + content["description"]
        content["owner"]["team"] = SCRIPT
        content["synonyms"]["en"].append(SCRIPT)
        for relation in content.get("relationships") or []:
            relation["description"] = SCRIPT
        for dimension in content.get("dimensions") or []:
            dimension["description"] = SCRIPT + " " + dimension["description"]
        for rule in content.get("rules") or []:
            rule["description"] = SCRIPT + " " + rule["description"]
        path.write_text(
            yaml.safe_dump(content, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    reindex(engine, root)
    app = w.make_app(engine.url)
    try:
        client = w.signed_in(app, w.make_user(engine, "viewer"))
        escaped = "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;"
        for url in (
            "/domains/lending",
            "/entities/contract",
            "/glossary/npf",
            "/metrics/penalty_income",
        ):
            response = client.get(url)
            assert response.status_code == 200, url
            assert "<script>alert" not in response.text, url
            assert all(s.get("src") for s in parsed(response.text).scripts), url
            assert escaped in response.text, url
            if url != "/domains/lending":
                assert escaped in section(response.text, "owner"), url
    finally:
        app.state.services.dispose()


# --- criterion 4: a deprecated metric ---


def test_a_deprecated_metric_page_shows_the_reason_and_links_to_the_replacement(
    fresh_service_world: tuple[Engine, Any], tmp_path: Path
) -> None:
    engine, users = fresh_service_world
    root = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, root)
    path = root / "lending" / "metrics" / "par30_ratio.yaml"
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    content["status"] = "deprecated"
    content["deprecation"] = {
        "reason": "Replaced by the NPF ratio, which the board reports.",
        "replaced_by": "metric:npf_ratio",
    }
    path.write_text(yaml.safe_dump(content, sort_keys=False, allow_unicode=True), encoding="utf-8")
    reindex(engine, root)
    app = w.make_app(engine.url)
    try:
        viewer = w.signed_in(app, w.make_user(engine, "viewer"))
        page = viewer.get("/metrics/par30_ratio")
        assert page.status_code == 200
        notice = section(page.text, "deprecation")
        assert "Replaced by the NPF ratio, which the board reports." in notice
        assert 'href="/metrics/npf_ratio"' in notice
        assert ">deprecated<" in section(page.text, "status")
        # Hidden from search and from the domain page, as the search service hides it.
        assert "par30_ratio" not in viewer.get("/domains/lending").text
        assert "/metrics/par30_ratio" not in viewer.get("/search", params={"q": "par30"}).text
    finally:
        app.state.services.dispose()


# --- errors ---


def test_a_database_error_in_a_discovery_page_is_the_fixed_500_page(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services.discovery import Discovery

    marker = "marker-secret-4111"

    def boom(self: Discovery, caller: Any) -> Any:
        raise RuntimeError(marker)

    monkeypatch.setattr(Discovery, "domains", boom)
    response = client_for(world, "viewer").get("/")
    assert response.status_code == 500
    assert "could not complete the request" in response.text
    assert marker not in response.text and "RuntimeError" not in response.text


def test_the_discovery_pages_are_in_the_navigation_and_every_route_needs_a_sign_in(
    world: Any,
) -> None:
    _, app = world
    paths = {path for path, _ in w.iter_routes(app)}
    assert {
        "/search",
        "/domains/{name}",
        "/metrics/{name}",
        "/entities/{name}",
        "/glossary/{name}",
    } <= paths
    signed_out = w.make_client(app)
    for path in ("/search?q=npf", "/domains/lending", "/metrics/npf_ratio", "/glossary/npf"):
        response = signed_out.get(path)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")
    hx = signed_out.get("/search?q=npf", headers={"HX-Request": "true"})
    assert hx.status_code == 401 and hx.headers["hx-redirect"] == "/login"
    nav = re.search(r"<nav.*?</nav>", client_for(world, "viewer").get("/").text, re.S)
    assert nav and 'href="/search"' in nav.group(0) and "/bench" not in nav.group(0)
