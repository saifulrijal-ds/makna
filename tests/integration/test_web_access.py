"""WP-16 acceptance 1, 3, 4, 5, 6 and the rulings on sessions, CSRF, `/mcp`, and error pages
(integration tier, on the real application)."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from makna.mcp.http import UNAUTHORIZED_BODY
from makna.services import accounts
from makna.services.accounts import AccountStore, AccountStoreError

from . import web_support as w

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "makna" / "web" / "static"
ROLES = ("viewer", "curator", "approver", "admin")
NO_DETAIL = "secret-detail-" + secrets.token_hex(4)


@pytest.fixture
def app(web_engine: Engine) -> Iterator[Any]:
    app = w.make_app(web_engine.url)
    yield app
    app.state.services.dispose()


# --- criterion 1: every route but /login, /healthz, and static files needs a sign-in ---


def test_every_route_but_login_healthz_and_static_redirects_to_login_when_signed_out(
    app: Any,
) -> None:
    client = w.make_client(app)
    routes = list(w.iter_routes(app))
    paths = {path for path, _ in routes}
    # The walk must see the real routes, or the loop below proves nothing.
    assert {"/", "/account", "/admin/users", "/admin/tokens", "/logout", "/login"} <= paths
    # The editor's routes (WP-19) are walked too: each of them must redirect when signed out.
    assert {
        "/new",
        "/new/{kind}",
        "/new/{kind}/save",
        "/edit/{kind}/{name}",
        "/edit/{kind}/{name}/submit",
    } <= paths
    public = {"/login", "/healthz", "/static"}
    checked = 0
    for path, methods in routes:
        if path in public:
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            response = client.request(method, w.fill(path))
            if path.startswith("/mcp"):
                # Not a web route: its own bearer-token check answers.
                assert response.status_code == 401, (method, path)
                continue
            assert response.status_code == 303, (method, path, response.status_code)
            assert response.headers["location"].startswith("/login"), (method, path)
            checked += 1
    assert checked >= 10
    # A path that does not exist is not told apart from one that does.
    assert client.get("/no/such/page").headers["location"].startswith("/login")


def test_login_healthz_and_static_files_need_no_sign_in(app: Any) -> None:
    client = w.make_client(app)
    assert client.get("/login").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/static/vendor/htmx/htmx.min.js").status_code == 200
    assert client.get("/static/vendor/pico/pico.classless.min.css").status_code == 200
    assert client.get("/static/makna.css").status_code == 200
    assert client.get("/static/discovery.css").status_code == 200
    # Nothing outside the static folder is served.
    assert client.get("/static/../routes.py").status_code in (303, 404)
    assert client.get("/static/%2e%2e/routes.py").status_code == 404


def test_a_htmx_request_without_a_session_is_told_to_navigate_to_the_login_page(app: Any) -> None:
    response = w.make_client(app).get("/account", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


# --- criterion 3: CSRF ---


def test_a_post_without_a_valid_csrf_token_is_rejected_with_403(
    web_engine: Engine, app: Any
) -> None:
    client = w.signed_in(app, w.make_user(web_engine))
    token = w.page_csrf(client)
    form = {"name": "from a test"}
    wrong = secrets.token_urlsafe(32)
    cases: dict[str, dict[str, Any]] = {
        "no token": {"data": form},
        "empty token": {"data": {**form, "csrf_token": ""}},
        "wrong token": {"data": {**form, "csrf_token": wrong}},
        "token of the login form": {"data": {**form, "csrf_token": "x" * 43}},
        "token twice": {"content": f"name=a&csrf_token={token}&csrf_token={token}".encode()},
        "token in the address": {"data": form, "params": {"csrf_token": token}},
        "wrong header": {"data": form, "headers": {"X-CSRF-Token": wrong}},
        "token in a json body": {
            "json": {**form, "csrf_token": token},
        },
        "token in a multipart body": {"files": {"csrf_token": (None, token), "name": (None, "x")}},
    }
    for label, arguments in cases.items():
        if "content" in arguments:
            arguments = {
                **arguments,
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            }
        response = client.post("/account/tokens", **arguments)
        assert response.status_code == 403, label
        assert "could not be verified" in response.text, label
    assert all(t.name != "from a test" for t in accounts.list_tokens(web_engine))
    # Every method that changes something is checked, not only POST.
    for method in ("PUT", "PATCH", "DELETE"):
        assert client.request(method, "/account/tokens").status_code == 403


def test_a_post_with_the_session_s_token_works_in_the_form_field_and_in_the_header(
    web_engine: Engine, app: Any
) -> None:
    client = w.signed_in(app, w.make_user(web_engine))
    token = w.page_csrf(client)
    assert (
        client.post("/account/tokens", data={"name": "by form", "csrf_token": token}).status_code
        == 200
    )
    response = client.post(
        "/account/tokens", data={"name": "by header"}, headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 200
    assert "by header" in response.text
    # The token of one session does not work in another.
    other = w.signed_in(app, w.make_user(web_engine))
    assert other.post("/account/tokens", data={"name": "x", "csrf_token": token}).status_code == 403


def test_the_token_is_in_every_form_and_in_the_htmx_headers(web_engine: Engine, app: Any) -> None:
    client = w.signed_in(app, w.make_user(web_engine, "admin"))
    for path in ("/account", "/admin/users", "/admin/tokens"):
        html = client.get(path).text
        token = w.csrf_in(html)
        assert f'"X-CSRF-Token": "{token}"' in html
        forms = re.findall(r"<form\b[^>]*method=\"post\"[^>]*>(.*?)</form>", html, re.S)
        assert forms
        assert all(f'name="csrf_token" value="{token}"' in form for form in forms), path


def test_the_login_form_is_protected_by_its_own_cookie(web_engine: Engine, app: Any) -> None:
    user = w.make_user(web_engine)
    client = w.make_client(app)
    form = {"email": user.email, "password": w.PASSWORD, "next": ""}
    # No login cookie, no token, a wrong token: all refused, and no password is checked.
    assert client.post("/login", data=form).status_code == 403
    page = client.get("/login")
    token = w.csrf_in(page.text)
    assert client.post("/login", data={**form, "csrf_token": "x" * 43}).status_code == 403
    assert client.post("/login", data=form).status_code == 403
    # A token from another browser's form does not fit this browser's cookie.
    other = w.make_client(app)
    other_token = w.csrf_in(other.get("/login").text)
    assert client.post("/login", data={**form, "csrf_token": other_token}).status_code == 403
    assert client.post("/login", data={**form, "csrf_token": token}).status_code == 303
    assert w.PROD.session in client.cookies


def test_the_form_page_reuses_the_login_cookie_so_two_tabs_both_work(app: Any) -> None:
    client = w.make_client(app)
    first = client.get("/login")
    assert w.PROD.login in first.headers["set-cookie"]
    second = client.get("/login")
    assert "set-cookie" not in second.headers
    assert w.csrf_in(first.text) == w.csrf_in(second.text)


def test_a_body_that_is_too_large_is_refused_before_it_is_read_whole(
    web_engine: Engine, app: Any
) -> None:
    client = w.signed_in(app, w.make_user(web_engine))
    huge = "name=" + "a" * (1024 * 1024 + 10)
    response = client.post(
        "/account/tokens",
        content=huge.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413


# --- criterion 4: the cookie ---


def attributes(header: str) -> dict[str, str]:
    parts = [part.strip() for part in header.split(";")]
    result = {"value": parts[0]}
    for part in parts[1:]:
        name, _, value = part.partition("=")
        result[name.lower()] = value
    return result


def test_the_session_cookie_is_httponly_samesite_lax_and_secure(
    web_engine: Engine, app: Any
) -> None:
    user = w.make_user(web_engine)
    client = w.make_client(app)
    page = client.get("/login")
    login_cookie = attributes(page.headers["set-cookie"])
    response = w.sign_in(client, user.email)
    cookies = response.headers.get_list("set-cookie")
    session = attributes(next(c for c in cookies if c.startswith(f"{w.PROD.session}=")))
    for cookie in (session, login_cookie):
        assert "httponly" in cookie
        assert cookie["samesite"].lower() == "lax"
        assert "secure" in cookie
        assert "domain" not in cookie  # host-only: sent to the host that set it, nowhere else
    assert session["path"] == "/"
    assert session["max-age"] == str(8 * 3600)
    assert login_cookie["path"] == "/"
    # The login cookie is removed once it has done its job.
    gone = attributes(next(c for c in cookies if c.startswith(f"{w.PROD.login}=")))
    assert gone["max-age"] == "0"


def test_in_development_mode_the_cookie_is_not_secure_but_still_httponly_and_lax(
    web_engine: Engine,
) -> None:
    app = w.make_app(web_engine.url, development=True)
    try:
        user = w.make_user(web_engine)
        client = w.make_client(app, development=True)
        response = w.sign_in(client, user.email)
        session = attributes(
            next(
                c
                for c in response.headers.get_list("set-cookie")
                if c.startswith(f"{w.DEV.session}=")
            )
        )
        assert "secure" not in session
        assert "httponly" in session and session["samesite"].lower() == "lax"
        assert client.get("/account").status_code == 200
    finally:
        app.state.services.dispose()


# --- criterion 5: no request to another host ---

ABSOLUTE = re.compile(r"https?://[^\s\"')>]+", re.I)
# The SVG namespace is an identifier inside the `data:` images of Pico, not an address.
NAMESPACES = {"http://www.w3.org/2000/svg"}


def test_pages_and_stylesheets_name_no_other_host(web_engine: Engine, app: Any) -> None:
    admin = w.signed_in(app, w.make_user(web_engine, "admin"))
    anonymous = w.make_client(app)
    pages = {
        "/login": anonymous.get("/login"),
        "/": admin.get("/"),
        "/account": admin.get("/account"),
        "/admin/users": admin.get("/admin/users"),
        "/admin/tokens": admin.get("/admin/tokens"),
        "404": admin.get("/no/such/page"),
        "403": w.signed_in(app, w.make_user(web_engine)).get("/admin/users"),
    }
    for name, response in pages.items():
        assert response.status_code in (200, 403, 404), name
        html = response.text
        assert not ABSOLUTE.findall(html), name
        # Every script, stylesheet, and form refers to a path of this application.
        for reference in re.findall(r'(?:src|href|action)="([^"]*)"', html):
            assert reference.startswith(("/", "#")) and not reference.startswith("//"), (
                name,
                reference,
            )
    for path in sorted(STATIC.glob("*.css")) + sorted(STATIC.glob("vendor/**/*.css")):
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        assert set(ABSOLUTE.findall(text)) <= NAMESPACES, path.name
    # The stylesheet and the scripts the pages name are really served.
    html = pages["/login"].text
    for reference in re.findall(r'(?:src|href)="(/static/[^"]*)"', html):
        assert anonymous.get(reference).status_code == 200, reference


# --- criterion 6: no Node toolchain ---


def test_the_repository_has_no_package_json_and_no_build_step() -> None:
    skipped = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}
    found = [
        path.relative_to(ROOT)
        for path in ROOT.rglob("*")
        if not skipped & set(path.relative_to(ROOT).parts)
        and path.name in {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
    ]
    assert found == []
    # The pages are served from files of the package as they are.
    assert (STATIC / "vendor" / "htmx" / "htmx.min.js").is_file()


# --- `/mcp` and the session cookie ---


def test_a_session_cookie_authenticates_nothing_under_mcp(web_engine: Engine, app: Any) -> None:
    admin = w.signed_in(app, w.make_user(web_engine, "admin"))
    assert admin.get("/account").status_code == 200
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    bare = w.make_client(app).post("/mcp", json=initialize, headers=headers)
    assert bare.status_code == 401
    for path in ("/mcp", "/mcp/"):
        with_cookie = admin.post(path, json=initialize, headers=headers)  # cookie and no bearer
        assert with_cookie.status_code == 401
        assert with_cookie.content == UNAUTHORIZED_BODY == bare.content
        assert admin.get(path).status_code == 401
        # The CSRF header does not turn a cookie into a credential either.
        csrf = {**headers, "X-CSRF-Token": w.page_csrf(admin)}
        assert admin.post(path, json=initialize, headers=csrf).status_code == 401
    assert "set-cookie" not in with_cookie.headers


def test_the_session_manager_still_starts_and_mcp_needs_no_csrf_token(
    web_engine: Engine, app: Any
) -> None:
    from fastapi.testclient import TestClient

    user = w.make_user(web_engine)
    token = accounts.create_token(web_engine, user.email, "mcp").token
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    with TestClient(app, base_url="http://testserver") as client:  # runs the lifespan
        assert client.get("/healthz").status_code == 200
        response = client.post(
            "/mcp",
            json=initialize,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert response.status_code == 200
        assert "serverInfo" in response.text
        # The web pages are the same application and still gated.
        assert client.get("/account", follow_redirects=False).status_code == 303


def test_no_cors_headers_are_ever_sent(web_engine: Engine, app: Any) -> None:
    client = w.signed_in(app, w.make_user(web_engine))
    for response in (
        client.get("/account", headers={"Origin": "https://evil.example"}),
        client.options("/account", headers={"Origin": "https://evil.example"}),
        w.make_client(app).get("/login", headers={"Origin": "https://evil.example"}),
    ):
        assert not [h for h in response.headers if h.lower().startswith("access-control-")]


# --- the user store decides on every request ---


def test_a_deactivated_user_is_signed_out_on_the_very_next_request(
    web_engine: Engine, app: Any
) -> None:
    user = w.make_user(web_engine, "admin")
    client = w.signed_in(app, user)
    assert client.get("/admin/users").status_code == 200
    accounts.deactivate_user(web_engine, user.email)
    response = client.get("/admin/users")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert "max-age=0" in response.headers["set-cookie"].lower()
    # A POST with a valid token is refused the same way.
    assert client.post("/account/tokens", data={"name": "x"}).status_code == 303
    assert w.sign_in(w.make_client(app), user.email).status_code == 401


def test_a_changed_role_applies_on_the_very_next_request(web_engine: Engine, app: Any) -> None:
    user = w.make_user(web_engine, "viewer")
    client = w.signed_in(app, user)
    assert client.get("/admin/users").status_code == 403
    assert "Users" not in client.get("/account").text
    accounts.update_user(web_engine, user.email, role="admin")
    assert client.get("/admin/users").status_code == 200
    assert "/admin/users" in client.get("/account").text
    accounts.update_user(web_engine, user.email, role="curator")
    assert client.get("/admin/users").status_code == 403


def test_a_copied_session_cookie_outlives_logout_but_not_deactivation(
    web_engine: Engine, app: Any
) -> None:
    """The limit written down in ARCHITECTURE section 13: a signed cookie cannot be revoked
    before it ends, so a copy made before sign-out still works until then. Deactivating the user
    is the stop that works at once (the test above)."""
    user = w.make_user(web_engine)
    client = w.signed_in(app, user)
    copy = client.cookies[w.PROD.session]
    token = w.page_csrf(client)
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get("/account").status_code == 303
    thief = w.make_client(app)
    w.plant(thief, w.PROD.session, copy)
    assert thief.get("/account").status_code == 200
    accounts.deactivate_user(web_engine, user.email)
    assert thief.get("/account").status_code == 303


def test_logout_needs_a_csrf_token(web_engine: Engine, app: Any) -> None:
    client = w.signed_in(app, w.make_user(web_engine))
    assert client.post("/logout").status_code == 403
    assert client.get("/account").status_code == 200
    assert client.get("/logout").status_code == 405


# --- error pages ---


def test_a_user_store_that_cannot_answer_gives_a_503_page_with_fixed_text(
    web_engine: Engine, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = w.signed_in(app, w.make_user(web_engine))

    def down(self: AccountStore, user_id: int, channel: str) -> Any:
        raise AccountStoreError

    monkeypatch.setattr(AccountStore, "caller_for_user", down)
    response = client.get("/account")
    assert response.status_code == 503
    assert "cannot reach its user store" in response.text
    # The user is not signed out by an outage.
    assert "max-age" not in response.headers.get("set-cookie", "")


def test_a_database_that_is_down_gives_a_503_page_that_names_nothing() -> None:
    url = "postgresql://makna:" + secrets.token_hex(6) + "@127.0.0.1:1/nothing"
    app = w.make_app(url)
    try:
        value, _ = app.state.web.codec.issue_session(1)
        client = w.make_client(app)
        w.plant(client, w.PROD.session, value)
        response = client.get("/account")
        assert response.status_code == 503
        for text in ("127.0.0.1", "postgresql", "nothing", "makna:", "Traceback", "psycopg"):
            assert text not in response.text
        # Sign-in is a 503 too, not a "wrong password".
        page = client.get("/login")
        failed = client.post(
            "/login",
            data={
                "csrf_token": w.csrf_in(page.text),
                "email": "a@demo.example",
                "password": w.PASSWORD,
            },
        )
        assert failed.status_code == 503
        assert w.PASSWORD not in failed.text
    finally:
        app.state.services.dispose()


def test_error_pages_carry_fixed_text_and_no_exception_text(
    web_engine: Engine, app: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = w.signed_in(app, w.make_user(web_engine))
    forbidden = client.get("/admin/users")
    assert forbidden.status_code == 403 and "do not have access" in forbidden.text
    missing = client.get("/no/such/page")
    assert missing.status_code == 404 and "does not exist" in missing.text
    # A malformed id is a page that does not exist.
    bad_id = client.post(
        "/account/tokens/abc/revoke", headers={"X-CSRF-Token": w.page_csrf(client)}
    )
    assert bad_id.status_code == 404
    wrong_method = client.get("/logout")
    assert wrong_method.status_code == 405 and "does not accept" in wrong_method.text
    assert w.make_client(app).post("/healthz").status_code == 405

    def boom(self: AccountStore, user_id: int) -> Any:
        raise RuntimeError(NO_DETAIL)

    monkeypatch.setattr(AccountStore, "get_user", boom)
    with caplog.at_level(logging.DEBUG):
        crashed = client.get("/account")
    assert crashed.status_code == 500
    assert "could not complete the request" in crashed.text
    for text in (NO_DETAIL, "RuntimeError", "Traceback", "boom"):
        assert text not in crashed.text
    for page in (forbidden, missing, wrong_method, crashed):
        assert page.headers["content-type"].startswith("text/html")
        assert page.headers["x-frame-options"] == "DENY"


def test_every_page_is_sent_with_the_security_headers_and_no_cache(
    web_engine: Engine, app: Any
) -> None:
    client = w.signed_in(app, w.make_user(web_engine))
    for response in (client.get("/account"), w.make_client(app).get("/login")):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["referrer-policy"] == "same-origin"
    assert "no-store" not in client.get("/static/makna.css").headers.get("cache-control", "")


def test_the_web_pages_do_not_check_the_host_header(app: Any) -> None:
    """Decision (ARCHITECTURE section 13): the cookies are host-only and the forms carry a token,
    so another `Host` reaches only the anonymous login page, as it would on any host."""
    client = w.make_client(app)
    assert client.get("/login", headers={"Host": "other.example"}).status_code == 200


# --- the navigation ---


def test_the_navigation_depends_on_the_role_and_links_only_to_pages_that_exist(
    web_engine: Engine, app: Any
) -> None:
    existing = {path for path, _ in w.iter_routes(app)}
    for role in ROLES:
        client = w.signed_in(app, w.make_user(web_engine, role))
        html = client.get("/account").text
        nav = re.search(r"<nav.*?</nav>", html, re.S)
        assert nav
        links = re.findall(r'href="([^"]+)"', nav.group(0))
        assert set(links) <= existing, (role, links)
        assert ("/admin/users" in links) == (role == "admin")
        assert ("/admin/tokens" in links) == (role == "admin")
        assert "/account" in links
        assert ("/new" in links) == (role != "viewer")


# --- fix round 1 ---


def test_a_crash_in_a_web_route_gives_the_500_page_and_is_not_raised_to_the_server(
    web_engine: Engine, app: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from fastapi.testclient import TestClient

    marker = "marker-" + secrets.token_hex(6)

    async def boom() -> None:
        raise RuntimeError(marker)

    app.add_api_route("/boom", boom, methods=["GET"])
    user = w.make_user(web_engine)
    client = TestClient(
        app, base_url="https://testserver", follow_redirects=False, raise_server_exceptions=True
    )
    assert w.sign_in(client, user.email).status_code == 303
    with caplog.at_level(logging.DEBUG):
        response = client.get("/boom")  # raises here if the error is passed on to the server
    assert response.status_code == 500
    assert "could not complete the request" in response.text
    assert marker not in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert marker not in caplog.text
    assert not [r for r in caplog.records if r.exc_info]
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


def test_the_cookies_use_the_host_prefix_outside_development_mode_only(
    web_engine: Engine, app: Any
) -> None:
    user = w.make_user(web_engine)
    client = w.make_client(app)
    first = client.get("/login")
    assert first.headers["set-cookie"].startswith("__Host-makna_login=")
    response = w.sign_in(client, user.email)
    cookies = response.headers.get_list("set-cookie")
    assert any(c.startswith("__Host-makna_session=") for c in cookies)
    # `__Host-` needs Secure, Path=/ and no Domain: all three are there.
    for cookie in cookies:
        attrs = attributes(cookie)
        assert "secure" in attrs and attrs["path"] == "/" and "domain" not in attrs
    # A plain `makna_session` cookie (planted by a sibling site, say) is not a session.
    thief = w.make_client(app)
    w.plant(thief, "makna_session", client.cookies[w.PROD.session])
    assert thief.get("/account").status_code == 303
    dev = w.make_app(web_engine.url, development=True)
    try:
        dev_client = w.make_client(dev, development=True)
        assert dev_client.get("/login").headers["set-cookie"].startswith("makna_login=")
    finally:
        dev.state.services.dispose()


def test_strict_transport_security_is_sent_outside_development_mode_only(
    web_engine: Engine, app: Any
) -> None:
    assert (
        w.make_client(app).get("/login").headers["strict-transport-security"].startswith("max-age=")
    )
    dev = w.make_app(web_engine.url, development=True)
    try:
        response = w.make_client(dev, development=True).get("/login")
        assert "strict-transport-security" not in response.headers
    finally:
        dev.state.services.dispose()


def test_no_path_is_redirected_to_a_host_taken_from_the_request(
    web_engine: Engine, app: Any
) -> None:
    client = w.signed_in(app, w.make_user(web_engine, "admin"))
    evil = {"Host": "evil.example"}
    for path in (
        "/account/",
        "/admin/users/",
        "/admin/tokens/",
        "/login/",
        "/static",
        "//evil.example",
    ):
        response = client.get(path, headers=evil)
        assert "evil.example" not in response.headers.get("location", ""), path
    assert client.get("/account/", headers=evil).status_code == 404
    # `/mcp` and `/mcp/` are routed by themselves and answer as before: 401 without a token.
    for path in ("/mcp", "/mcp/"):
        assert w.make_client(app).post(path, json={}).status_code == 401


def test_the_note_on_rotating_the_secret_key_is_in_the_architecture() -> None:
    text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Rotating `MAKNA_SECRET_KEY` is the only way to end all sessions at once" in text
