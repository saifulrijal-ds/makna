"""WP-19 (WEB-2): the editor routes `/edit/<kind>/<name>` and `/new/<kind>` with Check, Save draft
and Submit, on the real application with a temporary git repository (integration tier).

Acceptance 1 to 4 and the rulings: who may use it, CSRF, escaping, the size limit, the lost-update
check, the `/new` collision, a deprecated object, a plain directory, the policy, and the script.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from makna.definitions.loader import parse_yaml
from makna.definitions.render import MAX_YAML_BYTES
from makna.services import accounts
from makna.web import editor
from makna.web.gate import SECURITY_HEADERS

from . import lifecycle_support as sup
from . import web_support as w
from .lifecycle_support import World

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "makna" / "web" / "static"
NEW = "metric:contract_count_total"
NEW_DRAFT = "lending/metrics/contract_count_total.draft.yaml"
CHANGED = "metric:disbursement_amount"
TEAM = ("credit-risk",)
EDIT_ROUTES = [
    ("GET", "/new"),
    ("GET", "/new/metric"),
    ("GET", "/edit/metric/disbursement_amount"),
    *[("POST", f"/new/metric/{a}") for a in ("check", "save", "submit")],
    *[
        ("POST", f"/edit/metric/disbursement_amount/{a}")
        for a in ("check", "save", "submit", "withdraw")
    ],
]


@dataclass
class Ctx:
    world: World
    app: FastAPI
    engine: Engine

    def client(self, role: str = "curator", teams: tuple[str, ...] = TEAM, **kw: Any) -> Any:
        user = w.make_user(self.engine, role, teams=teams, **kw)
        client = w.signed_in(self.app, user)
        client.user = user  # type: ignore[attr-defined]
        return client


def build(url: URL, repo: Path, definitions: str | None = None) -> Iterator[Ctx]:
    world = sup.build_world(url, repo)
    app = w.make_app(url, definitions_repo=definitions or str(repo))
    try:
        yield Ctx(world, app, world.engine)
    finally:
        app.state.services.dispose()
        world.services.dispose()


@pytest.fixture
def ctx(empty_database: URL, tmp_path: Path) -> Iterator[Ctx]:
    yield from build(empty_database, tmp_path / "definitions")


@pytest.fixture(scope="module")
def shared(test_database_url: URL, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Ctx]:
    """One world for the tests that only read. They must leave `HEAD` and the tables alone."""
    name = f"makna_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(test_database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield from build(test_database_url.set(database=name), tmp_path_factory.mktemp("repo"))
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# --- helpers ---------------------------------------------------------------------------------


def box(page: str) -> str:
    """The text of the YAML box as a browser shows it (entities decoded)."""
    found = re.search(r'<textarea name="yaml_text"[^>]*>(.*?)</textarea>', page, re.S)
    assert found, "no text area"
    body = found.group(1)
    # A browser drops the one newline that follows the start tag (the page puts one there).
    return html.unescape(body.removeprefix("\n"))


def expected_of(page: str) -> str:
    found = re.search(r'name="expected" value="([^"]*)"', page)
    assert found, "no expected field"
    return html.unescape(found.group(1))


def post(client: Any, path: str, **fields: str) -> Any:
    return client.post(path, data={"csrf_token": w.page_csrf(client), **fields})


def form(client: Any, path: str, text_: str, reason: str = "a reason", **extra: str) -> Any:
    """What the browser sends for the editor's form: the text, the reason, and `expected` as the
    page carried it (for `/edit`, from the page; the caller can pass its own)."""
    return post(client, path, yaml_text=text_, reason=reason, **extra)


def issues_of(page: str) -> list[tuple[str, int | None]]:
    rows = re.findall(r'<tr data-rule="([^"]*)" data-line="([^"]*)"', page)
    return [(rule, int(line) if line else None) for rule, line in rows]


def own_metric(**changes: Any) -> str:
    return sup.metric_text(**changes)


def changed_text(world: World, **changes: Any) -> str:
    return sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(changes))


# --- who may use it --------------------------------------------------------------------------


def test_every_editor_route_needs_a_sign_in(shared: Ctx) -> None:
    client = w.make_client(shared.app)
    for method, path in EDIT_ROUTES:
        response = client.request(method, path)
        assert response.status_code == 303, (method, path)
        assert response.headers["location"].startswith("/login")


def test_a_viewer_gets_403_on_every_editor_route_before_any_name_is_looked_at(
    shared: Ctx,
) -> None:
    viewer = shared.client("viewer", teams=())
    before = sup.snapshot(shared.world)
    token = w.page_csrf(viewer)
    for method, path in EDIT_ROUTES:
        response = (
            viewer.get(path)
            if method == "GET"
            else viewer.post(path, data={"csrf_token": token, "yaml_text": "a: b", "reason": "r"})
        )
        assert response.status_code == 403, (method, path)
    # An unknown kind or name is a 403 too, not a 404 that tells a viewer what exists.
    for path in ("/edit/nothing/x", "/edit/metric/NOT_A_NAME", "/new/nothing"):
        assert viewer.get(path).status_code == 403, path
    assert viewer.post("/new/nothing/save", data={"csrf_token": token}).status_code == 403
    # A POST to an unknown kind or a malformed name is a 403 for a viewer too: the guard comes
    # before the name is looked at (a 404 would tell a viewer what exists).
    for path in (
        "/edit/nothing/x/save",
        "/edit/nothing/x/check",
        "/edit/metric/NOT_A_NAME/submit",
        "/edit/metric/NOT_A_NAME/withdraw",
        "/edit/metric/no_such_metric/save",
    ):
        response = viewer.post(path, data={"csrf_token": token, "yaml_text": "a: b", "reason": "r"})
        assert response.status_code == 403, path
    assert sup.snapshot(shared.world) == before


@pytest.mark.parametrize("role", ["curator", "approver", "admin"])
def test_curators_approvers_and_admins_can_open_the_editor(shared: Ctx, role: str) -> None:
    client = shared.client(role, teams=())
    for _, path in EDIT_ROUTES[:3]:
        assert client.get(path).status_code == 200, path


def test_the_navigation_and_the_edit_links_are_for_those_who_may_edit(shared: Ctx) -> None:
    viewer, curator = shared.client("viewer", teams=()), shared.client()
    assert 'href="/new"' not in viewer.get("/account").text
    assert '<a href="/new">New</a>' in curator.get("/account").text
    for kind, path, name in (
        ("metric", "/metrics/disbursement_amount", "disbursement_amount"),
        ("entity", "/entities/contract", "contract"),
        ("glossary_term", "/glossary/tenor", "tenor"),
    ):
        link = f'<a href="/edit/{kind}/{name}">Edit this definition</a>'
        assert link in curator.get(path).text, path
        assert "/edit/" not in viewer.get(path).text, path
    assert 'href="/new/metric"' in curator.get("/new").text


def test_the_pages_for_names_that_are_not_objects_are_404_and_do_not_echo_them(
    shared: Ctx,
) -> None:
    client = shared.client()
    marker = "marker_" + uuid.uuid4().hex[:8]
    for path in (
        f"/edit/metric/{marker}",  # a well-formed name that is no object
        "/edit/nothing/x",
        "/edit/metric/NOT_A_NAME",
        "/edit/metric/%2e%2e",
        "/edit/metric/a%2Fb",
        "/edit/metric/x%00y",
        f"/new/{marker}",
        "/new/metric%0A",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert marker not in response.text
    token = w.page_csrf(client)
    for path in (f"/edit/metric/{marker}/save", "/edit/nothing/x/check", f"/new/{marker}/submit"):
        response = client.post(path, data={"csrf_token": token, "yaml_text": "a: b", "reason": "r"})
        assert response.status_code == 404, path
        assert marker not in response.text


# --- CSRF ------------------------------------------------------------------------------------


def test_every_post_needs_the_csrf_token(shared: Ctx) -> None:
    client = shared.client()
    before = sup.snapshot(shared.world)
    token = w.page_csrf(client)
    fields = {"yaml_text": own_metric(), "reason": "r", "expected": "none"}
    for method, path in EDIT_ROUTES:
        if method != "POST":
            continue
        for label, extra in (
            ("none", {}),
            ("empty", {"csrf_token": ""}),
            ("wrong", {"csrf_token": "x" * 43}),
        ):
            response = client.post(path, data={**fields, **extra})
            assert response.status_code == 403, (path, label)
            assert "could not be verified" in response.text
        # The token in the address is not accepted.
        response = client.post(path, data=fields, params={"csrf_token": token})
        assert response.status_code == 403, path
    assert sup.snapshot(shared.world) == before
    # With the token, the same post is answered by the editor.
    assert client.post("/new/metric/check", data={**fields, "csrf_token": token}).status_code == 200


def test_a_get_changes_nothing(shared: Ctx) -> None:
    client = shared.client()
    before = sup.snapshot(shared.world)
    for path in (
        "/new",
        "/new/metric",
        "/new/entity",
        "/edit/metric/disbursement_amount",
        "/edit/entity/contract?notice=saved",
    ):
        assert client.get(path).status_code == 200
    assert sup.snapshot(shared.world) == before


# --- criterion 1: Check ----------------------------------------------------------------------


def test_check_shows_the_rule_the_line_and_the_message_and_writes_nothing(shared: Ctx) -> None:
    client = shared.client()
    before = sup.snapshot(shared.world)
    # An unknown key, at a line the test counts itself.
    bad_key = own_metric() + "colour: blue\n"
    line = bad_key.splitlines().index("colour: blue") + 1
    page = form(client, "/new/metric/check", bad_key).text
    assert ("V01", line) in issues_of(page)
    assert "colour" in page and "Nothing was saved." in page
    assert box(page).startswith("apiVersion")  # the text stays in the box
    # A V06 break in an existing entity: the measure's time dimension is not of type time.
    entity = sup.official_text(shared.world, "entity:contract_snapshot")
    broken = entity.replace("time_dimension: snapshot_date", "time_dimension: dpd_bucket", 1)
    line = [
        i + 1 for i, row in enumerate(broken.splitlines()) if "time_dimension: dpd_bucket" in row
    ][0]
    response = form(client, "/edit/entity/contract_snapshot/check", broken, expected="none")
    assert response.status_code == 200
    assert ("V06", line) in issues_of(response.text)
    assert "<code>V06</code>" in response.text
    assert sup.snapshot(shared.world) == before


def test_check_reports_a_clean_text_and_the_wrong_object(shared: Ctx) -> None:
    client = shared.client()
    ok = form(client, "/new/metric/check", own_metric()).text
    assert 'data-field="check-ok"' in ok and issues_of(ok) == []
    wrong = form(client, "/edit/metric/disbursement_amount/check", own_metric(), expected="none")
    assert [rule for rule, _ in issues_of(wrong.text)] == ["V02"]
    unreadable = form(client, "/new/metric/check", "name: [unclosed\n").text
    assert issues_of(unreadable)[0][0] == "V01"


def test_check_of_a_text_nested_too_deeply_is_a_fixed_issue(shared: Ctx) -> None:
    client = shared.client()
    deep = "a: " + "[" * 3000 + "]" * 3000 + "\n"
    page = form(client, "/edit/metric/disbursement_amount/check", deep, expected="none")
    assert page.status_code == 200
    assert [rule for rule, _ in issues_of(page.text)] == ["V01"]
    assert "nested too deeply" in page.text


def test_check_on_a_new_name_that_exists_points_to_its_editor(shared: Ctx) -> None:
    client = shared.client()
    page = form(client, "/new/metric/check", sup.official_text(shared.world, CHANGED)).text
    assert 'href="/edit/metric/disbursement_amount"' in page


# --- the pages -------------------------------------------------------------------------------


def test_an_object_page_loads_the_official_text_and_says_so(shared: Ctx) -> None:
    client = shared.client()
    page = client.get("/edit/metric/disbursement_amount").text
    assert "name: disbursement_amount" in box(page)
    assert "Loaded: the official version (number 1, status approved)" in page
    assert expected_of(page) == "none"
    assert "canonical form" in page
    assert 'formaction="/edit/metric/disbursement_amount/save"' in page
    assert 'formaction="/edit/metric/disbursement_amount/submit"' in page
    assert 'formaction="/edit/metric/disbursement_amount/check"' in page
    assert "The text is the same as the official version." in page


def test_the_page_has_the_text_area_and_one_script_file_for_each_script(shared: Ctx) -> None:
    client = shared.client()
    page = client.get("/edit/metric/disbursement_amount").text
    assert '<textarea name="yaml_text"' in page
    scripts = re.findall(r"<script[^>]*>", page)
    assert scripts and all('src="/static/' in tag and "defer" in tag for tag in scripts)
    assert [re.search(r'src="([^"]+)"', t).group(1) for t in scripts] == [  # type: ignore[union-attr]
        "/static/vendor/htmx/htmx.min.js",
        "/static/vendor/ace/ace.js",
        "/static/vendor/ace/mode-yaml.js",
        "/static/vendor/ace/theme-textmate.js",
        "/static/editor.js",
    ]
    assert not re.search(r"\son[a-z]+=", page)
    assert "<script>" not in page


def test_the_difference_view_is_side_by_side_rows_built_on_the_server(shared: Ctx) -> None:
    client = shared.client()
    changed = changed_text(shared.world, label="Disbursed <b>amount</b>")
    page = form(client, "/edit/metric/disbursement_amount/check", changed, expected="none").text
    rows = re.findall(r'<tr data-kind="(changed|added|removed)">', page)
    assert rows == ["changed"]
    assert "Disbursed &lt;b&gt;amount&lt;/b&gt;" in page and "<b>amount</b>" not in page
    assert "label: Disbursement Amount" in page  # the official side
    assert "unchanged line" in page  # the long unchanged runs are collapsed
    source = (ROOT / "src" / "makna" / "web" / "editor.py").read_text()
    assert "HtmlDiff(" not in source and "import HtmlDiff" not in source
    assert "|safe" not in (ROOT / "src" / "makna" / "web" / "templates" / "editor.html").read_text()


def test_a_new_object_has_no_difference_view(shared: Ctx) -> None:
    client = shared.client()
    page = form(client, "/new/metric/check", own_metric()).text
    assert "This is a new object, so there is no official version to compare with." in page
    assert 'data-field="diff"' not in page


# --- the policy and the script (criterion 4) -------------------------------------------------


EXPECTED_POLICY = (
    "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)


def test_the_policy_is_exactly_this_string_and_has_no_unsafe_source(shared: Ctx) -> None:
    client = shared.client()
    assert ("Content-Security-Policy", EXPECTED_POLICY) in SECURITY_HEADERS
    for path in ("/new/metric", "/edit/metric/disbursement_amount"):
        policy = client.get(path).headers["content-security-policy"]
        assert policy == EXPECTED_POLICY
    assert "unsafe" not in EXPECTED_POLICY and "blob:" not in EXPECTED_POLICY


def test_the_editor_script_is_small_and_builds_no_html_from_strings() -> None:
    script = (STATIC / "editor.js").read_text()
    code = re.sub(r"^\s*//.*$", "", script, flags=re.M)
    for forbidden in (
        "eval(",
        "Function(",
        "innerHTML",
        "outerHTML",
        "document.write",
        "http",
        "//",
    ):
        assert forbidden not in code, forbidden
    assert "useWorker: false" in code and 'basePath", "/static/vendor/ace"' in code
    assert 'useStrictCSP", true' in code
    assert "insertAdjacentHTML" not in code and "createContextualFragment" not in code
    assert len(script.splitlines()) < 60


def test_the_script_the_ace_styles_and_the_templates_are_served(shared: Ctx) -> None:
    client = shared.client()
    for path in (
        "/static/editor.js",
        "/static/editor.css",
        "/static/vendor/ace/ace.js",
        "/static/vendor/ace/mode-yaml.js",
        "/static/vendor/ace/theme-textmate.js",
        "/static/vendor/ace/css/ace.css",
        "/static/vendor/ace/css/theme/textmate.css",
        "/static/vendor/ace/css/textmate-1.png",
        "/static/vendor/ace/css/textmate-2.png",
    ):
        assert client.get(path).status_code == 200, path
    page = client.get("/new/metric").text
    for sheet in ("/static/vendor/ace/css/ace.css", "/static/vendor/ace/css/theme/textmate.css"):
        assert f'href="{sheet}"' in page


# --- criterion 3: templates ------------------------------------------------------------------

PLACEHOLDERS: dict[str, tuple[tuple[str | int, ...], ...]] = {
    "metric": (
        ("name",),
        ("label",),
        ("description",),
        ("domain",),
        ("owner", "team"),
        ("synonyms",),
        ("measure",),
    ),
    "entity": (
        ("name",),
        ("label",),
        ("description",),
        ("domain",),
        ("owner", "team"),
        ("synonyms",),
        ("source", "table"),
        ("primary_key",),
        ("dimensions", 0, "name"),
        ("dimensions", 0, "description"),
    ),
    "glossary_term": (
        ("name",),
        ("label",),
        ("description",),
        ("domain",),
        ("owner", "team"),
        ("synonyms",),
    ),
    "domain": (("name",), ("label",), ("description",), ("owner", "team")),
}
FILLED: dict[str, dict[str, Any]] = {
    "metric": {
        "name": "contract_count_total",
        "label": "Total Contract Count",
        "description": "The number of contracts in the portfolio on the last snapshot.",
        "domain": "lending",
        "synonyms": {"en": ["total contracts"], "id": ["total kontrak"]},
        "measure": "contract_snapshot.active_contracts",
    },
    "entity": {
        "name": "visit",
        "label": "Visit",
        "description": "One visit of a collector to a customer's address.",
        "domain": "lending",
        "synonyms": {"en": ["visit"], "id": ["kunjungan"]},
        "source": {"table": "lending.core.visits"},
        "primary_key": ["visit_id"],
        "dimensions": [
            {"name": "visit_date", "type": "time", "description": "The date of the visit."}
        ],
    },
    "glossary_term": {
        "name": "write_off",
        "label": "Write-off",
        "description": "A contract removed from the books because it will not be repaid.",
        "domain": "lending",
        "synonyms": {"en": ["charge-off"], "id": ["hapus buku"]},
    },
    "domain": {
        "name": "marketing",
        "label": "Marketing",
        "description": "Campaigns, leads and the cost of acquiring customers.",
    },
}


@pytest.mark.parametrize("kind", ["domain", "entity", "metric", "glossary_term"])
def test_a_template_fails_only_on_the_fields_the_author_must_fill(shared: Ctx, kind: str) -> None:
    from makna.definitions import layout

    assert kind in layout.KINDS
    body = editor.template_text(kind, None)
    _, lines = parse_yaml(body)
    placeholder_lines = {lines.line_of(path) for path in PLACEHOLDERS[kind]}
    curator = shared.world.caller("curator")
    name = f"template_{kind}"
    result = shared.world.life.check(
        curator, f"{kind}:{name}", body.replace('name: ""', f"name: {name}", 1)
    )
    assert not result.ok
    assert result.issues, "a template with nothing to fill would be a definition by itself"
    for issue in result.issues:
        assert issue.rule == "V01"
        assert issue.line in placeholder_lines, (kind, issue)
    # Filling the fields makes it pass.
    data = yaml.safe_load(body)
    data.update(FILLED[kind])
    data["owner"] = {"team": "credit-risk"}
    filled = yaml.safe_dump(data, sort_keys=False)
    good = shared.world.life.check(curator, f"{kind}:{FILLED[kind]['name']}", filled)
    assert good.ok, [(i.rule, i.line, i.message) for i in good.issues]


def test_the_new_page_starts_from_the_template_with_the_callers_team(shared: Ctx) -> None:
    curator = shared.client(teams=("credit-risk", "sales-performance"))
    page = curator.get("/new/metric").text
    shown = box(page)
    assert shown == editor.template_text("metric", "credit-risk")
    assert yaml.safe_load(shown)["owner"] == {"team": "credit-risk"}
    assert expected_of_missing(page)
    teamless = shared.client("admin", teams=())
    assert yaml.safe_load(box(teamless.get("/new/metric").text))["owner"] == {"team": ""}
    # A team called something that is not plain text is still a valid YAML string.
    odd = editor.template_text("metric", 'a "team": {x}\\\n#')
    assert yaml.safe_load(odd)["owner"] == {"team": 'a "team": {x}\\\n#'}
    for kind in ("domain", "entity", "glossary_term"):
        assert curator.get(f"/new/{kind}").status_code == 200


def expected_of_missing(page: str) -> bool:
    """The `/new` form has no `expected` field: its routes always expect a new object."""
    return 'name="expected"' not in page


def test_checking_the_template_names_what_to_fill_in(shared: Ctx) -> None:
    client = shared.client()
    template = box(client.get("/new/metric").text)
    page = form(client, "/new/metric/check", template).text
    rules = issues_of(page)
    assert rules and all(rule == "V01" for rule, _ in rules)
    assert box(page) == template  # the text stays


# --- criterion 2: save and submit make the WP-18 commits -------------------------------------


def commit_message(world: World, sha: str) -> list[str]:
    return world.repo._git(["log", "-1", "--format=%B", sha]).decode().splitlines()


def new_commits(world: World, before: int) -> list[Any]:
    return world.repo.log()[before:]


def audit_after(world: World, count: int) -> list[Any]:
    return sup.audit_rows(world.engine)[count:]


def stored_draft(world: World, path: str = NEW_DRAFT) -> str:
    return (world.repo.show(sup.head(world), path) or b"").decode()


def test_save_draft_makes_the_commit_of_the_signed_in_user_and_the_audit_row(ctx: Ctx) -> None:
    client = ctx.client(name="Dewi Curator")
    user = client.user
    commits, audits = len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))
    response = form(client, "/new/metric/save", own_metric(), reason="A first draft")
    assert response.status_code == 303
    assert response.headers["location"] == "/edit/metric/contract_count_total?notice=saved"
    made = new_commits(ctx.world, commits)
    assert len(made) == 1
    assert (made[0].author_name, made[0].author_email) == (user.display_name, user.email)
    lines = commit_message(ctx.world, made[0].sha)
    assert lines[0] == f"propose({NEW}): A first draft"
    assert lines[2] == "Makna-Action: create_proposal"
    assert lines[4] == f"Makna-Actor: {user.id}"
    rows = audit_after(ctx.world, audits)
    assert [(r["action"], r["object_id"], r["reason"]) for r in rows] == [
        ("create_proposal", NEW, "A first draft")
    ]
    assert rows[0]["actor_user_id"] == user.id and rows[0]["commit_sha"] == made[0].sha
    assert "name: contract_count_total" in stored_draft(ctx.world)
    # The page the browser is sent to holds what was stored, and says it was saved.
    page = client.get(response.headers["location"]).text
    assert "The draft was saved." in page
    assert box(page) == stored_draft(ctx.world)
    assert "Loaded: the open proposal (status draft)" in page
    assert len(expected_of(page)) == 64


def test_submit_saves_first_when_the_text_differs_then_submits(ctx: Ctx) -> None:
    client = ctx.client()
    user = client.user
    commits, audits = len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))
    response = form(client, "/new/metric/submit", own_metric(), reason="Ready for review")
    assert response.status_code == 303
    assert response.headers["location"] == "/edit/metric/contract_count_total?notice=submitted"
    made = new_commits(ctx.world, commits)
    assert [commit_message(ctx.world, c.sha)[0] for c in made] == [
        f"propose({NEW}): Ready for review",
        f"submit({NEW}): Ready for review",
    ]
    assert all(c.author_email == user.email for c in made)
    rows = audit_after(ctx.world, audits)
    assert [(r["action"], r["reason"], r["actor_user_id"]) for r in rows] == [
        ("create_proposal", "Ready for review", user.id),
        ("submit", "Ready for review", user.id),
    ]
    assert "status: in_review" in stored_draft(ctx.world)
    page = client.get(response.headers["location"]).text
    assert "The proposal was submitted for review." in page
    assert "Loaded: the open proposal (status in review)" in page


def test_submit_of_the_stored_text_makes_one_commit_and_a_changed_text_two(ctx: Ctx) -> None:
    client = ctx.client()
    form(client, "/new/metric/save", own_metric(), reason="draft")
    page = client.get("/edit/metric/contract_count_total").text
    base = len(ctx.world.repo.log())
    # The box as it was loaded, posted back with the browser's CRLF line breaks: nothing changed.
    same = form(
        client,
        "/edit/metric/contract_count_total/submit",
        box(page).replace("\n", "\r\n"),
        reason="ready",
        expected=expected_of(page),
    )
    assert same.status_code == 303
    assert [commit_message(ctx.world, c.sha)[0] for c in new_commits(ctx.world, base)] == [
        f"submit({NEW}): ready"
    ]
    # An edit of the returned-to-draft proposal, then submit: the edit and the submit.
    page = client.get("/edit/metric/contract_count_total").text
    edited = box(page).replace("label: Total Contract Count", "label: All Contracts")
    base = len(ctx.world.repo.log())
    response = form(
        client,
        "/edit/metric/contract_count_total/submit",
        edited,
        reason="again",
        expected=expected_of(page),
    )
    assert response.status_code == 303
    assert [commit_message(ctx.world, c.sha)[0] for c in new_commits(ctx.world, base)] == [
        f"edit({NEW}): again",
        f"submit({NEW}): again",
    ]
    assert "label: All Contracts" in stored_draft(ctx.world)


def test_a_change_to_an_official_object_is_a_proposal_then_an_edit_of_it(ctx: Ctx) -> None:
    client = ctx.client(teams=("sales-performance",))
    page = client.get("/edit/metric/disbursement_amount").text
    assert expected_of(page) == "none"
    first = form(
        client,
        "/edit/metric/disbursement_amount/save",
        changed_text(ctx.world, label="Disbursed"),
        reason="a label",
        expected="none",
    )
    assert first.status_code == 303
    page = client.get(first.headers["location"]).text
    assert "Loaded: the open proposal (status draft)" in page
    assert "The official version is number 1." in page
    assert re.findall(r'<tr data-kind="(changed|added|removed)"', page) == ["changed"]
    second = form(
        client,
        "/edit/metric/disbursement_amount/save",
        changed_text(ctx.world, label="Disbursed amount"),
        reason="a better label",
        expected=expected_of(page),
    )
    assert second.status_code == 303
    actions = [r["action"] for r in sup.audit_rows(ctx.engine)[-2:]]
    assert actions == ["create_proposal", "update_proposal"]


# --- the lost update -------------------------------------------------------------------------


def test_two_people_editing_one_proposal_do_not_overwrite_each_other(ctx: Ctx) -> None:
    first = ctx.client("approver", teams=())
    second = ctx.client("approver", teams=())
    form(first, "/new/metric/save", own_metric(), reason="draft")
    seen_by_first = first.get("/edit/metric/contract_count_total").text
    seen_by_second = second.get("/edit/metric/contract_count_total").text
    assert expected_of(seen_by_first) == expected_of(seen_by_second)
    theirs = box(seen_by_first).replace("label: Total Contract Count", "label: Theirs")
    mine = box(seen_by_second).replace("label: Total Contract Count", "label: Mine")
    saved = form(
        first,
        "/edit/metric/contract_count_total/save",
        theirs,
        reason="theirs",
        expected=expected_of(seen_by_first),
    )
    assert saved.status_code == 303
    commits, audits = len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))
    clash = form(
        second,
        "/edit/metric/contract_count_total/save",
        mine,
        reason="mine",
        expected=expected_of(seen_by_second),
    )
    assert clash.status_code == 409
    assert 'data-code="CONFLICT"' in clash.text and "changed after you opened it" in clash.text
    assert box(clash.text) == mine  # what was typed stays in the box
    assert 'href="/edit/metric/contract_count_total"' in clash.text
    assert (len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))) == (commits, audits)
    assert "label: Theirs" in stored_draft(ctx.world)
    # The page for the conflict keeps the old fingerprint, so a second try conflicts again.
    assert expected_of(clash.text) == expected_of(seen_by_second)
    again = form(
        second,
        "/edit/metric/contract_count_total/submit",
        mine,
        reason="mine",
        expected=expected_of(clash.text),
    )
    assert again.status_code == 409 and len(ctx.world.repo.log()) == commits


def test_a_proposal_that_appeared_after_the_page_was_opened_is_a_conflict_too(ctx: Ctx) -> None:
    mine, theirs = (
        ctx.client(teams=("sales-performance",)),
        ctx.client(teams=("sales-performance",)),
    )
    page = mine.get("/edit/metric/disbursement_amount").text
    form(
        theirs,
        "/edit/metric/disbursement_amount/save",
        changed_text(ctx.world, label="Theirs"),
        reason="theirs",
        expected="none",
    )
    commits = len(ctx.world.repo.log())
    clash = form(
        mine,
        "/edit/metric/disbursement_amount/save",
        changed_text(ctx.world, label="Mine"),
        reason="mine",
        expected=expected_of(page),
    )
    assert clash.status_code == 409 and len(ctx.world.repo.log()) == commits


def test_a_form_without_a_valid_expected_value_is_refused(ctx: Ctx) -> None:
    client = ctx.client(teams=("sales-performance",))
    commits = len(ctx.world.repo.log())
    for value in ("", "x", "A" * 64):
        response = form(
            client,
            "/edit/metric/disbursement_amount/save",
            changed_text(ctx.world, label="Mine"),
            expected=value,
        )
        assert response.status_code == 400, value
        assert 'data-code="INVALID_INPUT"' in response.text
    assert len(ctx.world.repo.log()) == commits


# --- /new that is not new --------------------------------------------------------------------


def test_a_new_object_with_the_name_of_an_existing_one_is_refused_with_a_link(ctx: Ctx) -> None:
    client = ctx.client(teams=("sales-performance",))
    commits, audits = len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))
    existing = sup.official_text(ctx.world, CHANGED)
    for action in ("save", "submit"):
        response = form(client, f"/new/metric/{action}", existing)
        assert response.status_code == 409, action
        assert "already exists" in response.text
        assert 'href="/edit/metric/disbursement_amount"' in response.text
        assert box(response.text) == existing
    # A name that exists only as a proposal is just as taken.
    form(ctx.client(), "/new/metric/save", own_metric(), reason="draft")
    commits, audits = len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))
    again = form(ctx.client(), "/new/metric/save", own_metric(), reason="again")
    assert again.status_code == 409 and 'href="/edit/metric/contract_count_total"' in again.text
    assert (len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))) == (commits, audits)


def test_a_new_object_with_text_that_cannot_be_read_is_not_saved(ctx: Ctx) -> None:
    client = ctx.client()
    commits = len(ctx.world.repo.log())
    response = form(client, "/new/metric/save", "name: [unclosed\n")
    assert response.status_code == 422
    assert "cannot be saved yet" in response.text and issues_of(response.text)[0][0] == "V01"
    assert box(response.text) == "name: [unclosed\n"
    assert len(ctx.world.repo.log()) == commits


# --- what the lifecycle refuses is shown, with the text kept -------------------------------


def test_a_refusal_of_the_lifecycle_is_shown_with_its_code_and_the_text_stays(ctx: Ctx) -> None:
    outsider = ctx.client(teams=("marketing",))
    commits = len(ctx.world.repo.log())
    response = form(outsider, "/new/metric/save", own_metric(), reason="not my team")
    assert response.status_code == 403
    assert 'data-code="PERMISSION_DENIED"' in response.text
    assert "must be owned by one of your teams" in response.text
    assert box(response.text) == own_metric().replace("\r\n", "\n")
    assert len(ctx.world.repo.log()) == commits
    # A reason that is missing or too long is refused before anything is written.
    mine = ctx.client()
    for reason in ("", "   ", "x" * 501, "two\nlines"):
        response = form(mine, "/new/metric/save", own_metric(), reason=reason)
        assert response.status_code == 400, repr(reason)
        assert 'data-code="INVALID_INPUT"' in response.text and "reason" in response.text
    assert len(ctx.world.repo.log()) == commits


def test_a_submit_the_lifecycle_refuses_after_the_save_goes_on_editing_the_saved_draft(
    ctx: Ctx,
) -> None:
    client = ctx.client()
    commits = len(ctx.world.repo.log())
    # Readable (V01 passes) but its measure does not exist (V08): the draft is saved, the submit
    # is refused, and the page says both.
    broken = own_metric(measure="contract_snapshot.no_such_measure")
    response = form(client, "/new/metric/submit", broken, reason="try")
    assert response.status_code == 422
    assert 'data-code="DEFINITION_INVALID"' in response.text
    assert "The draft was saved, but it was not submitted." in response.text
    assert any(rule == "V08" for rule, _ in issues_of(response.text))
    assert len(ctx.world.repo.log()) == commits + 1
    # The form now belongs to the saved draft, with its fingerprint.
    assert 'formaction="/edit/metric/contract_count_total/save"' in response.text
    assert len(expected_of(response.text)) == 64
    fixed = form(
        client,
        "/edit/metric/contract_count_total/submit",
        own_metric(),
        reason="fixed",
        expected=expected_of(response.text),
    )
    assert fixed.status_code == 303


# --- a deprecated object and a plain directory -----------------------------------------------


def test_a_deprecated_object_is_refused_with_a_message_and_offers_no_save(ctx: Ctx) -> None:
    retired = "metric:penalty_income"  # nothing depends on it, so it can be deprecated
    ctx.world.life.deprecate(ctx.world.caller("approver"), retired, "retired")
    client = ctx.client(teams=("sales-performance",))
    page = client.get("/edit/metric/penalty_income").text
    assert "A deprecated object cannot be changed." in page
    assert 'data-field="blocked"' in page
    assert "/save" not in page and "/submit" not in page and "/check" in page
    commits = len(ctx.world.repo.log())
    text_ = sup.edited(sup.official_text(ctx.world, retired), lambda d: d.update(label="Again"))
    for action in ("save", "submit"):
        response = form(client, f"/edit/metric/penalty_income/{action}", text_, expected="none")
        assert response.status_code == 409, action
        assert "A deprecated object cannot be changed." in response.text
        assert box(response.text) == text_
    assert len(ctx.world.repo.log()) == commits


def test_a_proposal_of_another_author_is_shown_as_not_yours_to_change(ctx: Ctx) -> None:
    author, other = ctx.client(), ctx.client()
    form(author, "/new/metric/save", own_metric(), reason="mine")
    page = other.get("/edit/metric/contract_count_total").text
    assert "Only the author of this proposal can change it." in page
    assert "/save" not in page
    approver = ctx.client("approver", teams=())
    assert "/save" in approver.get("/edit/metric/contract_count_total").text


def test_a_plain_directory_refuses_every_write_and_says_so(ctx: Ctx) -> None:
    from tests.catalog_support import DEFINITIONS

    app = w.make_app(ctx.engine.url, definitions_repo=str(DEFINITIONS))
    try:
        user = w.make_user(ctx.engine, "curator", teams=TEAM)
        client = w.signed_in(app, user)
        commits, audits = len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))
        for path in ("/new/metric", "/edit/metric/disbursement_amount"):
            response = client.get(path)
            assert response.status_code == 409, path
            assert "plain directory" in response.text and "cannot be changed here" in response.text
            assert 'name="yaml_text"' not in response.text
        for action in ("check", "save", "submit"):
            for path in (f"/new/metric/{action}", f"/edit/metric/disbursement_amount/{action}"):
                response = form(client, path, own_metric(), expected="none")
                assert response.status_code == 409, path
                assert "plain directory" in response.text, path
        assert (len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))) == (commits, audits)
    finally:
        app.state.services.dispose()


# --- escaping --------------------------------------------------------------------------------

HOSTILE = "</textarea><script>alert(1)</script>\"'&<img src=x onerror=alert(2)>"


def assert_inert(page: str) -> None:
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "</textarea><script>" not in page
    assert "<img" not in page and "<svg" not in page
    for tag in re.findall(r"<script[^>]*>", page):
        assert 'src="/static/' in tag


def test_yaml_issues_and_the_reason_are_escaped_everywhere(ctx: Ctx) -> None:
    client = ctx.client()
    hostile_yaml = own_metric() + f"{HOSTILE.replace(chr(10), ' ')}: 1\n"
    page = form(client, "/new/metric/check", hostile_yaml, reason=HOSTILE).text
    assert_inert(page)
    assert html.unescape(box(page)) == hostile_yaml or box(page) == hostile_yaml
    assert "&lt;/textarea&gt;&lt;script&gt;" in page  # in the box and in the issue message
    assert box(page).count("</textarea>") == 1  # the box did not close early
    assert 'value="&lt;/textarea&gt;&lt;script&gt;alert(1)&lt;/script&gt;&#34;&#39;&amp;' in page
    # A refusal page escapes them too, and the box keeps them.
    refused = form(client, "/new/metric/save", hostile_yaml, reason=HOSTILE + "\n")
    assert refused.status_code in (400, 422)
    assert_inert(refused.text)
    # A text that is valid YAML with hostile values in a difference row.
    changed = changed_text(ctx.world, caveats=HOSTILE)
    diff = form(client, "/edit/metric/disbursement_amount/check", changed, expected="none").text
    assert_inert(diff)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in diff


def test_a_hostile_object_name_is_not_echoed(ctx: Ctx) -> None:
    client = ctx.client()
    bad = own_metric(name="<script>alert(1)</script>")
    page = form(client, "/new/metric/check", bad).text
    assert_inert(page)
    other = form(client, "/edit/metric/disbursement_amount/check", bad, expected="none").text
    assert_inert(other)


# --- size ------------------------------------------------------------------------------------


def test_a_text_over_the_limit_is_a_413_page_that_does_not_echo_it(ctx: Ctx) -> None:
    client = ctx.client()
    commits = len(ctx.world.repo.log())
    marker = "marker-" + uuid.uuid4().hex
    over = "# " + marker + "\n" + "#" * MAX_YAML_BYTES
    for path in (
        "/new/metric/check",
        "/new/metric/save",
        "/edit/metric/disbursement_amount/submit",
    ):
        response = form(client, path, over, expected="none")
        assert response.status_code == 413, path
        assert marker not in response.text and "marker-" not in response.text
    # Bytes count, not characters: this text has fewer characters than the limit.
    wide = "é" * (MAX_YAML_BYTES // 2 + 1)
    assert len(wide) < MAX_YAML_BYTES
    assert form(client, "/new/metric/check", wide).status_code == 413
    # A body over what the gate reads is a 413 too.
    huge = form(client, "/new/metric/check", "#" * (2 * 1024 * 1024))
    assert huge.status_code == 413
    assert len(ctx.world.repo.log()) == commits
    # Exactly the limit is accepted (it is not a definition, so Check lists V01).
    exact = "#" * MAX_YAML_BYTES
    response = form(client, "/new/metric/check", exact)
    assert response.status_code == 200 and issues_of(response.text)[0][0] == "V01"
    # The reason has its own limit, shown as a problem with the reason.
    long = form(client, "/new/metric/save", own_metric(), reason="r" * 501)
    assert long.status_code == 400 and "at most 500 characters" in long.text
    assert len(ctx.world.repo.log()) == commits


# --- unexpected errors -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda s: __import__("sqlalchemy").exc.SQLAlchemyError(s), id="database"),
        pytest.param(lambda s: RecursionError(s), id="recursion"),
        pytest.param(
            lambda s: __import__("makna.definitions.repo").definitions.repo.RepoError("undo", 128),
            id="repository",
        ),
        pytest.param(lambda s: RuntimeError(s), id="other"),
    ],
)
def test_an_unexpected_error_is_the_500_page_and_its_text_goes_nowhere(
    ctx: Ctx,
    error: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from makna.services.lifecycle import Lifecycle

    secret = "secret-" + uuid.uuid4().hex

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise error(secret)

    for name in ("create_proposal", "update_proposal", "submit", "check", "editor_source"):
        monkeypatch.setattr(Lifecycle, name, boom)
    client = ctx.client()
    with caplog.at_level(logging.DEBUG):
        responses = [
            client.get("/edit/metric/disbursement_amount"),
            form(client, "/new/metric/save", own_metric()),
            form(client, "/new/metric/submit", own_metric()),
            form(client, "/new/metric/check", own_metric()),
            form(client, "/edit/metric/disbursement_amount/save", own_metric(), expected="none"),
        ]
    for response in responses:
        assert response.status_code == 500
        assert "could not complete the request" in response.text
        assert secret not in response.text and "Traceback" not in response.text
    assert secret not in caplog.text
    assert not [r for r in caplog.records if r.exc_info]
    assert any("web: editor" in r.getMessage() for r in caplog.records)
    assert not any(secret in r.getMessage() for r in caplog.records)


def test_every_code_a_lifecycle_call_can_refuse_with_has_a_message_and_a_status() -> None:
    from makna.services import LifecycleErrorCode, ServiceErrorCode

    codes = {str(c) for c in ServiceErrorCode} | {str(c) for c in LifecycleErrorCode}
    assert codes <= set(editor.HEADLINES) and codes <= set(editor.STATUS_OF)
    assert all(editor.HEADLINES[c].endswith(".") for c in codes)


# --- logging ---------------------------------------------------------------------------------


def test_the_log_names_the_user_and_the_object_but_never_the_text_or_the_reason(
    ctx: Ctx, caplog: pytest.LogCaptureFixture
) -> None:
    client = ctx.client()
    marker = "marker-" + uuid.uuid4().hex
    with caplog.at_level(logging.INFO):
        form(client, "/new/metric/check", own_metric(description=marker + " " * 30))
        form(client, "/new/metric/save", own_metric(description=marker + " " * 30), reason=marker)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("web: editor")]
    assert len(lines) == 2
    assert f"object={NEW}" in lines[1] and f"user={client.user.id}" in lines[1]
    assert marker not in caplog.text


# --- criterion 4: no script ------------------------------------------------------------------


def test_with_no_script_the_text_area_is_the_editor_and_the_buttons_are_form_posts(
    ctx: Ctx,
) -> None:
    """Every request of these tests is a plain form post, which is all a browser without
    JavaScript sends; the page's own buttons name their routes with `formaction`."""
    client = ctx.client()
    page = client.get("/new/metric").text
    assert re.search(r'<form method="post" action="/new/metric/check"', page)
    buttons = re.findall(r'<button type="submit" formaction="([^"]+)"', page)
    assert buttons == ["/new/metric/check", "/new/metric/save", "/new/metric/submit"]
    assert '<textarea name="yaml_text"' in page
    user = accounts.get_user_by_id(ctx.engine, client.user.id)
    assert user is not None
    commits = len(ctx.world.repo.log())
    saved = form(client, buttons[1], box(page).replace('name: ""', "name: from_a_template"))
    assert saved.status_code == 422  # still has fields to fill: the template alone is not saved
    done = form(client, buttons[1], own_metric(), reason="no script")
    assert done.status_code == 303 and len(ctx.world.repo.log()) == commits + 1


# --- fix round 1 -----------------------------------------------------------------------------


def test_a_failure_after_the_commit_says_so_and_never_claims_nothing_was_changed(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services import store

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("secret-index-error")

    monkeypatch.setattr(store, "sync", broken)
    client = ctx.client()
    commits = len(ctx.world.repo.log())
    response = form(client, "/new/metric/save", own_metric(), reason="committed, not indexed")
    assert response.status_code == 500
    assert len(ctx.world.repo.log()) == commits + 1  # the commit was made
    page = html.unescape(response.text)
    assert "The change was committed but the index could not be updated" in page
    assert "makna index sync" in page
    assert "nothing was changed" not in page.lower() and "Nothing was changed" not in page
    assert "secret-index-error" not in page
    assert box(response.text) == own_metric()  # the typed text stays


def test_a_rewritten_history_gives_the_409_message_on_every_editor_page(ctx: Ctx) -> None:
    client = ctx.client()
    sup.git(ctx.world.repo, "commit", "--amend", "-m", "rewritten history", "--allow-empty")
    commits = len(ctx.world.repo.log())
    page = client.get("/edit/metric/disbursement_amount")
    assert page.status_code == 409 and 'data-code="CONFLICT"' in page.text
    assert "history" in page.text and 'name="yaml_text"' not in page.text
    for path in (
        "/new/metric/check",
        "/new/metric/save",
        "/edit/metric/disbursement_amount/check",
        "/edit/metric/disbursement_amount/save",
        "/edit/metric/disbursement_amount/submit",
    ):
        response = form(client, path, own_metric(), expected="none")
        assert response.status_code == 409, path
        assert "history" in response.text, path
        assert box(response.text) == own_metric(), path  # the text of a POST is kept
    assert len(ctx.world.repo.log()) == commits


def test_a_text_that_starts_with_a_newline_comes_back_as_it_was_sent(shared: Ctx) -> None:
    client = shared.client()
    for lead in ("\n", "\n\n", "\r\n\r\n"):
        sent = lead + own_metric()
        page = form(client, "/new/metric/check", sent).text
        assert box(page) == sent.replace("\r\n", "\n")
        assert "<textarea" in page and ">\n" + sent.replace("\r\n", "\n").split("\n")[0] in page
    # With the first lines blank, the issue lines of the next Check are those of what was shown.
    page = form(client, "/new/metric/check", "\n\n" + own_metric() + "colour: blue\n").text
    line = (box(page).splitlines().index("colour: blue")) + 1
    assert ("V01", line) in issues_of(page)


def test_a_hostile_official_text_is_escaped_in_the_left_column_of_the_difference(ctx: Ctx) -> None:
    retired = "metric:penalty_income"
    hostile = "</td><script>alert(1)</script><img src=x onerror=alert(2)> and more words"
    text = sup.edited(sup.official_text(ctx.world, retired), lambda d: d.update(caveats=hostile))
    author = ctx.world.caller("curator")
    ctx.world.life.create_proposal(author, retired, text, "hostile")
    ctx.world.life.submit(author, retired, "ready")
    ctx.world.life.approve(ctx.world.caller("approver"), retired, "ok")
    client = ctx.client()
    edited = sup.edited(text, lambda d: d.update(caveats="plain"))
    page = form(client, "/edit/metric/penalty_income/check", edited, expected="none").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page  # the official side, escaped
    assert_inert(page)
    assert_inert(client.get("/edit/metric/penalty_income").text)


def test_the_issue_list_of_a_new_text_that_cannot_be_read_is_capped_and_counted(
    shared: Ctx,
) -> None:
    client = shared.client()
    text = own_metric() + "".join(f"unknown_key_{i}: 1\n" for i in range(300))
    for path in ("/new/metric/check", "/new/metric/save", "/new/metric/submit"):
        response = form(client, path, text)
        assert len(issues_of(response.text)) == 50, path
        assert "300 problems found" in response.text or path != "/new/metric/check"
    page = form(client, "/new/metric/check", text).text
    assert "Only the first 50 are shown." in page


def test_a_number_with_too_many_digits_is_a_v01_issue_and_the_text_stays(ctx: Ctx) -> None:
    client = ctx.client()
    digits = "9" * 5000
    text = own_metric() + f"extra_number: {digits}\n"
    line = text.splitlines().index(f"extra_number: {digits}") + 1
    commits = len(ctx.world.repo.log())
    for path, extra in (
        ("/new/metric/check", {}),
        ("/edit/metric/disbursement_amount/check", {"expected": "none"}),
    ):
        response = form(client, path, text, **extra)
        assert response.status_code == 200, path
        assert ("V01", line) in issues_of(response.text) and "too many digits" in response.text
        assert box(response.text) == text
    response = form(client, "/new/metric/save", text)
    assert response.status_code == 422 and box(response.text) == text
    response = form(client, "/edit/metric/disbursement_amount/save", text, expected="none")
    assert response.status_code in (400, 422) and box(response.text) == text
    assert len(ctx.world.repo.log()) == commits


def test_an_unreadable_proposal_offers_check_only_and_no_empty_box_to_overwrite(ctx: Ctx) -> None:
    path = ctx.world.repo.path / "lending" / "metrics" / "huge_one.draft.yaml"
    path.write_text("# " + "a" * (MAX_YAML_BYTES + 10) + "\n")
    sup.git(ctx.world.repo, "add", "-A")
    sup.git(ctx.world.repo, "commit", "-m", "outside huge file")
    admin = ctx.client("admin", teams=())
    page = admin.get("/edit/metric/huge_one").text
    assert "cannot be read here" in page and 'data-field="blocked"' in page
    assert "/save" not in page and "/submit" not in page and "/check" in page
    assert expected_of(page) != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_an_entity_change_that_check_passes_is_saved_submitted_and_approved(ctx: Ctx) -> None:
    """Check and approval agree: V06, V13 and V19 (the metrics that read the entity and their
    example requests) are run by both, and a clean Check means the gate of approval passes."""
    object_id = "entity:contract_snapshot"
    changed = sup.edited(
        sup.official_text(ctx.world, object_id),
        lambda d: d.update(description=d["description"] + " Reviewed in the editor."),
    )
    client = ctx.client(teams=("credit-risk",))
    path = "/edit/entity/contract_snapshot"
    checked = form(client, path + "/check", changed, expected="none")
    assert checked.status_code == 200 and 'data-field="check-ok"' in checked.text
    saved = form(client, path + "/submit", changed, reason="describe it", expected="none")
    assert saved.status_code == 303
    approved = ctx.world.life.approve(ctx.world.caller("approver"), object_id, "looks right")
    assert (approved.status, approved.version) == ("approved", 2)
    # A change that breaks a metric is refused by Check and by approval alike.
    broken = sup.edited(
        sup.official_text(ctx.world, object_id),
        lambda d: d.update(measures=[m for m in d["measures"] if m["name"] != "active_contracts"]),
    )
    result = form(client, path + "/check", broken, expected="none")
    assert issues_of(result.text) and 'data-own="no"' in result.text


def test_the_script_blocks_every_module_load_and_leaves_the_editor_by_keyboard() -> None:
    script = (STATIC / "editor.js").read_text()
    assert "ace.config.setLoader(function () {})" in script
    assert "enableKeyboardAccessibility: true" in script
    for command in ("find", "replace", "showSettingsMenu", "openCommandPalette", "gotoline"):
        assert f'"{command}"' in script
    css = (STATIC / "editor.css").read_text()
    assert "prefers-color-scheme: dark" in css and 'data-theme="dark"' in css


def test_the_page_says_how_to_leave_the_editor_with_the_keyboard(shared: Ctx) -> None:
    page = shared.client().get("/new/metric").text
    assert "press Esc and then Tab" in page


# --- fix round 2 -----------------------------------------------------------------------------


def test_the_parsing_of_the_editor_runs_off_the_event_loop(
    shared: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    seen: list[str] = []

    def off_the_loop(real: Any, name: str) -> Any:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:  # no running loop: a worker thread, as it must be
                seen.append(name)
                return real(*args, **kwargs)
            raise AssertionError(f"{name} ran on the event loop")

        return guarded

    monkeypatch.setattr(editor, "_name_in", off_the_loop(editor._name_in, "_name_in"))
    monkeypatch.setattr(editor, "make_diff", off_the_loop(editor.make_diff, "make_diff"))
    client = shared.client()
    for path, extra in (
        ("/new/metric/check", {}),
        ("/new/metric/save", {}),
        ("/edit/metric/disbursement_amount/check", {"expected": "none"}),
    ):
        response = form(client, path, own_metric() + "colour: blue\n", **extra)
        assert response.status_code in (200, 422), (path, response.status_code)
    assert client.get("/edit/metric/disbursement_amount").status_code == 200
    assert {"_name_in", "make_diff"} <= set(seen)


def test_an_integer_over_the_limit_in_any_base_is_listed_by_the_editor_and_the_text_stays(
    ctx: Ctx,
) -> None:
    client = ctx.client()
    commits = len(ctx.world.repo.log())
    for value in ("9" * 5000, "0x" + "f" * 4000, "0" + "7" * 5000, "0b" + "1" * 20000):
        text = (
            own_metric()
            + f"filters:\n  - {{dimension: contract_snapshot.branch, op: '=', value: {value}}}\n"
        )
        line = text.splitlines().index(next(r for r in text.splitlines() if value in r)) + 1
        for path, extra in (
            ("/new/metric/check", {}),
            ("/edit/metric/disbursement_amount/check", {"expected": "none"}),
        ):
            response = form(client, path, text, **extra)
            assert response.status_code == 200, (value[:6], path)
            assert ("V01", line) in issues_of(response.text)
            assert box(response.text) == text
        for path, extra in (
            ("/new/metric/save", {}),
            ("/edit/metric/disbursement_amount/save", {"expected": "none"}),
        ):
            response = form(client, path, text, **extra)
            assert response.status_code in (400, 422), (value[:6], path)
            assert box(response.text) == text
    assert len(ctx.world.repo.log()) == commits


def test_the_page_for_too_many_open_proposals_keeps_the_text(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services import lifecycle

    monkeypatch.setattr(lifecycle, "MAX_OPEN_DRAFTS_PER_USER", 1)
    client = ctx.client()
    assert form(client, "/new/metric/save", own_metric(), reason="first").status_code == 303
    second = own_metric(name="second_metric")
    commits = len(ctx.world.repo.log())
    response = form(client, "/new/metric/save", second, reason="second")
    assert response.status_code == 409 and 'data-code="TOO_MANY_DRAFTS"' in response.text
    assert "the most one user may have" in response.text and box(response.text) == second
    assert len(ctx.world.repo.log()) == commits


def test_check_names_a_text_that_would_be_too_large_once_approved(shared: Ctx) -> None:
    from .test_editor_service import stored_description

    client = shared.client()
    text = stored_description(shared.world, MAX_YAML_BYTES - 100)  # the draft fits
    assert len(text.encode()) < MAX_YAML_BYTES
    page = form(client, "/new/metric/check", text).text
    assert "approved (with its version)" in page and "deprecated (with a reason)" in page


def withdraw_post(client: Any, base: str, reason: str, expected: str | None = None) -> Any:
    """Withdraw as the page does it: with the fingerprint of the proposal the page showed."""
    if expected is None:
        page = client.get(base)
        # The fingerprint the withdraw form itself carries, not the main form's.
        found = re.search(r'<form[^>]*data-field="withdraw".*?</form>', page.text, re.S)
        own = found.group(0) if found else ""
        expected = expected_of(own) if 'name="expected"' in own else "none"
    return post(client, base + "/withdraw", reason=reason, expected=expected)


# --- withdraw --------------------------------------------------------------------------------


def test_the_author_withdraws_a_new_proposal_through_the_page(ctx: Ctx) -> None:
    client = ctx.client()
    form(client, "/new/metric/save", own_metric(), reason="draft")
    page = client.get("/edit/metric/contract_count_total").text
    assert (
        'data-field="withdraw"' in page
        and 'action="/edit/metric/contract_count_total/withdraw"' in page
    )
    commits, audits = len(ctx.world.repo.log()), len(sup.audit_rows(ctx.engine))
    # No reason: refused, nothing changes.
    refused = withdraw_post(client, "/edit/metric/contract_count_total", " ")
    assert refused.status_code == 400 and 'data-code="INVALID_INPUT"' in refused.text
    assert len(ctx.world.repo.log()) == commits
    done = withdraw_post(client, "/edit/metric/contract_count_total", "not needed")
    assert done.status_code == 303 and done.headers["location"] == "/new?notice=withdrawn"
    assert "The proposal was withdrawn." in client.get(done.headers["location"]).text
    made = new_commits(ctx.world, commits)
    assert [commit_message(ctx.world, c.sha)[0] for c in made] == [f"withdraw({NEW}): not needed"]
    assert made[0].author_email == client.user.email
    assert [(r["action"], r["reason"]) for r in audit_after(ctx.world, audits)] == [
        ("withdraw", "not needed")
    ]
    assert client.get("/edit/metric/contract_count_total").status_code == 404


def test_withdrawing_a_change_to_an_official_object_goes_back_to_its_editor(ctx: Ctx) -> None:
    client = ctx.client(teams=("sales-performance",))
    form(
        client,
        "/edit/metric/disbursement_amount/save",
        changed_text(ctx.world, label="Disbursed"),
        expected="none",
    )
    done = withdraw_post(client, "/edit/metric/disbursement_amount", "changed my mind")
    assert done.status_code == 303
    assert done.headers["location"] == "/edit/metric/disbursement_amount?notice=withdrawn"
    page = client.get(done.headers["location"]).text
    assert "The proposal was withdrawn." in page and 'data-field="withdraw"' not in page


def test_only_those_the_lifecycle_allows_may_withdraw(ctx: Ctx) -> None:
    author, other = ctx.client(), ctx.client()
    form(author, "/new/metric/save", own_metric(), reason="mine")
    path = "/edit/metric/contract_count_total"
    assert 'data-field="withdraw"' not in other.get(path).text  # not offered to a non-author
    commits = len(ctx.world.repo.log())
    refused = withdraw_post(other, path, "mine now")
    assert refused.status_code == 403 and 'data-code="PERMISSION_DENIED"' in refused.text
    assert "Only the author of this proposal can change it." in refused.text
    viewer = ctx.client("viewer", teams=())
    assert withdraw_post(viewer, path, "r").status_code == 403
    assert len(ctx.world.repo.log()) == commits
    approver = ctx.client("approver", teams=())
    assert 'data-field="withdraw"' in approver.get(path).text
    assert withdraw_post(approver, path, "cleaning up").status_code == 303
    assert len(ctx.world.repo.log()) == commits + 1


def test_a_missing_object_or_proposal_is_not_withdrawn(ctx: Ctx) -> None:
    client = ctx.client()
    assert withdraw_post(client, "/edit/metric/no_such_metric", "r").status_code == 404
    response = withdraw_post(client, "/edit/metric/disbursement_amount", "r")
    assert response.status_code == 409 and 'data-code="CONFLICT"' in response.text


def test_an_unreadable_proposal_has_a_way_out_and_cannot_be_overwritten_by_a_forged_save(
    ctx: Ctx,
) -> None:
    path = ctx.world.repo.path / "lending" / "metrics" / "huge_one.draft.yaml"
    path.write_text("# " + "a" * (MAX_YAML_BYTES + 10) + "\n")
    sup.git(ctx.world.repo, "add", "-A")
    sup.git(ctx.world.repo, "commit", "-m", "outside huge file")
    admin = ctx.client("admin", teams=())
    page = admin.get("/edit/metric/huge_one").text
    assert 'data-field="withdraw"' in page and "/save" not in page
    commits = len(ctx.world.repo.log())
    forged = form(
        admin,
        "/edit/metric/huge_one/save",
        own_metric(name="huge_one"),
        expected=expected_of(page),  # the fingerprint the page carried
    )
    assert forged.status_code == 409 and "cannot be read here" in forged.text
    assert len(ctx.world.repo.log()) == commits
    done = withdraw_post(admin, "/edit/metric/huge_one", "unreadable")
    assert done.status_code == 303 and len(ctx.world.repo.log()) == commits + 1
    assert admin.get("/edit/metric/huge_one").status_code == 404


def test_a_too_large_outside_edit_of_an_official_file_can_be_withdrawn_and_restores_it(
    ctx: Ctx,
) -> None:
    official_path = ctx.world.repo.path / "lending" / "metrics" / "penalty_income.yaml"
    original = official_path.read_text()
    official_path.write_text(original + "# " + "a" * (MAX_YAML_BYTES + 10) + "\n")
    sup.git(ctx.world.repo, "add", "-A")
    sup.git(ctx.world.repo, "commit", "-m", "outside edit of an official file")
    admin = ctx.client("admin", teams=())
    page = admin.get("/edit/metric/penalty_income").text
    assert 'data-field="withdraw"' in page
    done = withdraw_post(admin, "/edit/metric/penalty_income", "restore")
    assert done.status_code == 303
    assert official_path.read_text() == original
    assert (
        ctx.world.repo.show(sup.head(ctx.world), "lending/metrics/penalty_income.yaml")
        == original.encode()
    )


def test_a_withdraw_from_a_stale_page_is_refused_and_the_newer_draft_survives(ctx: Ctx) -> None:
    author = ctx.client()
    approver = ctx.client("approver", teams=())
    form(author, "/new/metric/save", own_metric(), reason="draft")
    path = "/edit/metric/contract_count_total"
    stale = approver.get(path).text  # the approver's page, before the author saves again
    newer = box(author.get(path).text).replace("label: Total Contract Count", "label: Newer")
    form(author, path + "/save", newer, reason="newer", expected=expected_of(author.get(path).text))
    commits = len(ctx.world.repo.log())
    refused = withdraw_post(approver, path, "clean up", expected=expected_of(stale))
    assert refused.status_code == 409 and 'data-code="CONFLICT"' in refused.text
    assert "changed after you opened it" in refused.text
    assert len(ctx.world.repo.log()) == commits and "label: Newer" in stored_draft(ctx.world)
    # The page with the current fingerprint withdraws it; a forged form without one is refused.
    assert withdraw_post(approver, path, "clean up", expected="").status_code == 400
    assert withdraw_post(approver, path, "clean up").status_code == 303
