"""WP-20 acceptance 5: `/bench` shows the blocks of section 13 in order, an edited request
recompiles, a compile error is shown with its code and details, the SQL can be copied, and the
bench never runs it (D-21). Plus the rulings on escaping, CSRF, the size of the JSON, and the
clock. The index uses the hash provider, so the metric a question reaches is checked only where
the words leave no doubt."""

from __future__ import annotations

import datetime
import html
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy import Engine, text

from makna.web import bench as bench_module
from makna.web.gate import SECURITY_HEADERS

from . import web_support as w
from .service_support import Users

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "makna" / "web" / "static"
ROLES = ("viewer", "curator", "approver", "admin")
QUESTION = "What is the average ticket size by region last month?"
TODAY = datetime.date(2026, 7, 1)


@pytest.fixture
def app(service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setattr(bench_module, "today", lambda: TODAY)
    engine, _ = service_world
    app = w.make_app(engine.url)
    yield app
    app.state.services.dispose()


@pytest.fixture
def engine(service_world: tuple[Engine, Users]) -> Engine:
    return service_world[0]


def client_for(app: Any, engine: Engine, role: str = "viewer") -> TestClient:
    return w.signed_in(app, w.make_user(engine, role))


def post(client: TestClient, request_json: str, q: str = QUESTION, **extra: str) -> Any:
    return client.post(
        "/bench",
        data={
            "csrf_token": w.page_csrf(client, "/bench"),
            "request_json": request_json,
            "q": q,
            **extra,
        },
    )


def box_text(page: str) -> str:
    """The text of the request box as a browser shows it (entities decoded)."""
    box = re.search(r'<textarea name="request_json"[^>]*>(.*?)</textarea>', page, re.S)
    assert box is not None
    return html.unescape(box.group(1))


def order(page: str, *markers: str) -> list[int]:
    found = [page.find(marker) for marker in markers]
    assert all(i >= 0 for i in found), (markers, found)
    return found


# --- who may use it, and the page --------------------------------------------------------------


def test_the_bench_needs_a_sign_in(app: Any) -> None:
    client = w.make_client(app)
    response = client.get("/bench?q=npf")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert client.post("/bench", data={"request_json": "{}"}).status_code == 303


@pytest.mark.parametrize("role", ROLES)
def test_every_signed_in_role_can_use_the_bench(app: Any, engine: Engine, role: str) -> None:
    client = client_for(app, engine, role)
    response = client.get("/bench")
    assert response.status_code == 200
    assert 'name="q"' in response.text and 'name="request_json"' in response.text
    assert '<a href="/bench">Bench</a>' in client.get("/").text


def test_the_page_shows_the_blocks_in_order(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    page = client.get("/bench", params={"q": QUESTION}).text
    # 1 the question box, 2 the search hits, 3 the resolution, 4 the request, 5 the SQL.
    positions = order(
        page,
        'name="q"',
        "<h2>Search hits</h2>",
        "<h2>Resolution</h2>",
        "<h2>Request</h2>",
        "<h2>SQL</h2>",
        "<h3>Explanation</h3>",
    )
    assert positions == sorted(positions)
    assert "average_ticket_size" in page
    assert "<th>Score</th>" in page and "<th>Matched terms</th>" in page
    assert "Today is 2026-07-01" in page
    assert "2026-06-01" in page and "2026-07-01" in page  # last month, from the server's date
    assert "branch.region_name" in page
    assert "SELECT" in page.upper()


def test_the_request_box_holds_the_request_the_resolver_built(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    page = client.get("/bench", params={"q": QUESTION}).text
    box = box_text(page)
    assert '"metric": "average_ticket_size"' in box
    assert '"time_start": "2026-06-01"' in box


def test_the_date_comes_from_the_server_clock_once_at_the_edge(
    app: Any, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = client_for(app, engine)
    monkeypatch.setattr(bench_module, "today", lambda: datetime.date(2027, 3, 15))
    page = client.get("/bench", params={"q": QUESTION}).text
    assert "Today is 2027-03-15" in page
    assert "2027-02-01" in page and "2027-03-01" in page


def test_an_ambiguous_question_chooses_nothing(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    page = client.get("/bench", params={"q": "NPF"}).text
    assert "<mark>ambiguous</mark>" in page
    assert "Nothing was chosen" in page
    assert '"metric": ""' in box_text(page)
    assert "<h2>SQL</h2>" not in page


def test_a_question_that_matches_nothing_says_so(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    page = client.get("/bench", params={"q": "employee attrition by region"}).text
    assert "no match" in page
    assert "do not use a different metric" in page


def test_a_draft_is_never_a_candidate_even_for_an_admin(app: Any, engine: Engine) -> None:
    client = client_for(app, engine, "admin")
    page = client.get("/bench", params={"q": "hapus buku write-off"}).text
    assert "write_off_amount" not in page


def test_an_over_long_question_is_an_error_not_a_crash(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    response = client.get("/bench", params={"q": "x" * 501})
    assert response.status_code == 200
    assert "INVALID_INPUT" in response.text
    assert "x" * 501 not in response.text.replace('value="' + "x" * 501, "")


# --- escaping ---------------------------------------------------------------------------------

EVIL = "<script>alert(1)</script> \"quoted\" 'single' & <img src=x onerror=alert(2)>"


def test_the_question_is_escaped_everywhere_it_appears(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    page = client.get("/bench", params={"q": EVIL}).text
    assert "<script>alert(1)" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page
    # The only scripts on the page are files of this origin.
    for tag in re.findall(r"<script[^>]*>", page):
        assert "src=" in tag, tag
    # In the text box and in the hidden field of the form, the question is one escaped attribute.
    escaped = str(escape(EVIL))
    assert page.count(f'value="{escaped}"') == 2
    assert "<img" not in page


def test_a_request_with_markup_in_it_is_escaped_in_the_box_and_in_the_error(
    app: Any, engine: Engine
) -> None:
    client = client_for(app, engine)
    request = (
        '{"metric": "<script>alert(3)</script>", "filters": [{"dimension": "\\"><b>x</b>", '
        '"op": "=", "value": "<i>v</i>"}]}'
    )
    response = post(client, request, q=EVIL)
    page = response.text
    assert response.status_code == 200
    assert "UNKNOWN_METRIC" in page
    assert "<script>alert(3)" not in page and "<b>x</b>" not in page and "<i>v</i>" not in page
    assert "&lt;script&gt;alert(3)" in page
    assert "<script>alert(1)" not in page


def test_sql_and_explanation_text_is_escaped(app: Any, engine: Engine) -> None:
    template = (ROOT / "src" / "makna" / "web" / "templates" / "bench.html").read_text()
    assert "|safe" not in template.replace(" ", "") and "Markup" not in template
    source = (ROOT / "src" / "makna" / "web" / "bench.py").read_text()
    assert "Markup" not in source and "|safe" not in source and "autoescape" not in source


# --- editing the request ----------------------------------------------------------------------


def test_posting_an_edited_request_compiles_it_again(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    request = '{"metric": "average_ticket_size", "grain": "month", "time_start": "2026-01-01"}'
    response = post(client, request)
    assert response.status_code == 200
    page = response.text
    assert "<h2>SQL</h2>" in page and "<h3>Explanation</h3>" in page
    assert "2026-01-01" in page
    assert '"time_start": "2026-01-01"' in box_text(page)  # the text you sent stays in the box
    # The question's resolution is shown again above the request.
    assert (
        page.index("<h2>Resolution</h2>")
        < page.index("<h2>Request</h2>")
        < page.index("<h2>SQL</h2>")
    )


def test_a_compile_error_is_shown_with_its_code_and_details(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    page = post(client, '{"metric": "no_such_metric"}').text
    assert "<code>UNKNOWN_METRIC</code>" in page
    assert "no_such_metric" in page
    assert "<pre><code>" in page  # the details block
    page = post(client, '{"metric": "average_ticket_size", "dimensions": ["branch.no_such"]}').text
    assert "UNKNOWN_DIMENSION" in page


def test_a_request_that_is_not_json_is_refused_with_the_place(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    response = post(client, '{"metric": ')
    assert response.status_code == 400
    assert "This is not valid JSON" in response.text and "line 1" in response.text
    assert "<h2>SQL</h2>" not in response.text


@pytest.mark.parametrize(
    ("request_json", "message"),
    [
        ('{"metric": "average_ticket_size", "sql": "select 1"}', "sql: Extra inputs"),
        ('{"metric": 5}', "metric:"),
        ('{"dimensions": []}', "metric: Field required"),
        ('{"metric": "x", "limit": true}', "limit:"),
        ("[1, 2]", "must be a JSON object"),
        ('"text"', "must be a JSON object"),
        ("[" * 10000, "nested too deeply"),
    ],
)
def test_a_request_with_the_wrong_shape_is_refused(
    app: Any, engine: Engine, request_json: str, message: str
) -> None:
    client = client_for(app, engine)
    response = post(client, request_json)
    assert response.status_code == 400
    assert message in response.text
    assert "<h2>SQL</h2>" not in response.text


def test_a_request_over_16_kib_is_refused_and_not_echoed(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    big = '{"metric": "' + "m" * 17000 + '"}'
    response = post(client, big)
    assert response.status_code == 413
    assert "longer than 16 KiB" in response.text
    assert "m" * 100 not in response.text
    just_under = '{"metric": "' + "m" * 16000 + '"}'
    assert post(client, just_under).status_code == 200  # an unknown metric, not a size problem


def test_the_post_needs_the_csrf_token(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    body = {"request_json": '{"metric": "average_ticket_size"}', "q": QUESTION}
    assert client.post("/bench", data=body).status_code == 403
    assert client.post("/bench", data={**body, "csrf_token": "wrong"}).status_code == 403
    assert client.post("/bench", data=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    ok = client.post("/bench", data={**body, "csrf_token": w.page_csrf(client, "/bench")})
    assert ok.status_code == 200
    # A token in the query string does not count.
    token = w.page_csrf(client, "/bench")
    assert client.post(f"/bench?csrf_token={token}", data=body).status_code == 403


def test_a_get_changes_no_definition_user_or_setting(app: Any, engine: Engine) -> None:
    tables = ("semantic_object", "app_user", "api_token", "index_state", "audit_event")

    def counts() -> list[Any]:
        with engine.connect() as connection:
            return [
                connection.execute(text(f"SELECT count(*), max(0) FROM {t}")).one() for t in tables
            ] + [
                connection.execute(
                    text("SELECT md5(string_agg(key || value, ',' ORDER BY key)) FROM index_state")
                ).scalar_one()
            ]

    client = client_for(app, engine)
    before = counts()
    client.get("/bench", params={"q": QUESTION})
    client.get("/bench", params={"q": "NPF"})
    after = counts()
    # `app_user` grew by the one user this test made before `before` was taken: nothing since.
    assert after == before


def test_both_calls_are_logged_as_the_signed_in_user(app: Any, engine: Engine) -> None:
    user = w.make_user(engine, "curator")
    client = w.signed_in(app, user)
    client.get("/bench", params={"q": QUESTION})
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT tool, channel, status FROM tool_call_log WHERE user_id = :u ORDER BY id"),
            {"u": user.id},
        ).all()
    assert [(r.tool, r.channel, r.status) for r in rows] == [
        ("resolve", "web", "ok"),
        ("compile_query", "web", "ok"),
    ]


# --- the copy button and the content security policy ------------------------------------------


def test_the_copy_button_is_one_static_file_and_the_page_works_without_it(
    app: Any, engine: Engine
) -> None:
    client = client_for(app, engine)
    page = client.get("/bench", params={"q": QUESTION}).text
    assert 'id="copy-sql"' in page and "hidden" in page.split('id="copy-sql"')[1].split(">")[0]
    assert '<script src="/static/bench.js" defer></script>' in page
    assert re.search(r'<pre><code id="sql-text">(WITH|SELECT)', page, re.I)  # the SQL is plain text
    served = client.get("/static/bench.js")
    assert served.status_code == 200
    script = (STATIC / "bench.js").read_text()
    assert served.text == script
    assert "eval(" not in script and "Function(" not in script
    assert "http" not in script and "//" not in re.sub(r"^\s*//.*$", "", script, flags=re.M)
    assert "innerHTML" not in script and "document.write" not in script


def test_the_policy_still_forbids_inline_script(app: Any, engine: Engine) -> None:
    client = client_for(app, engine)
    response = client.get("/bench", params={"q": QUESTION})
    policy = response.headers["content-security-policy"]
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert ("Content-Security-Policy", policy) in SECURITY_HEADERS
    assert "script-src" not in policy and "default-src 'self'" in policy
    for tag in re.findall(r"<script[^>]*>", response.text):
        assert 'src="/static/' in tag
    assert not re.search(r"\son[a-z]+=", response.text)  # no inline handlers


def test_the_bench_has_no_route_that_runs_sql(app: Any) -> None:
    paths = {path for path, _ in w.iter_routes(app) if path.startswith("/bench")}
    assert paths == {"/bench"}
    methods = {m for path, ms in w.iter_routes(app) if path == "/bench" for m in ms}
    assert methods == {"GET", "POST"}
