"""WP-16 acceptance 2 and the rulings on sign-in (integration tier, on the real application).

Sign-in: one message for every failure, the throttle, no password or email in the logs, the
`next` path, a new session at every sign-in, and what the session cookie holds.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine

from makna.app import create_app
from makna.services import accounts
from makna.web.routes import BAD_LOGIN, THROTTLED

from . import web_support as w


@pytest.fixture
def app(web_engine: Engine) -> Iterator[Any]:
    app = w.make_app(web_engine.url)
    yield app
    app.state.services.dispose()


def error_text(html: str) -> str:
    found = re.search(r'role="alert"><mark>([^<]*)<', html)
    assert found, html
    return found.group(1)


def without_email(html: str) -> str:
    """The page without the refilled email and the form's own CSRF value, so that two answers can
    be compared."""
    html = re.sub(r'(name="email" value=")[^"]*"', r'\1"', html)
    return re.sub(r'(name="csrf_token" value=")[^"]*"', r'\1"', html)


# --- criterion 2: one message for every failure ---


def test_a_wrong_password_an_unknown_email_and_an_inactive_user_get_the_same_answer(
    web_engine: Engine, app: Any
) -> None:
    known = w.make_user(web_engine)
    gone = w.make_user(web_engine)
    accounts.deactivate_user(web_engine, gone.email)
    other = secrets.token_urlsafe(16)
    cases = {
        "wrong password": (known.email, other),
        "unknown email": (f"nobody-{secrets.token_hex(3)}@demo.example", w.PASSWORD),
        "inactive user": (gone.email, w.PASSWORD),
        "not an email": ("not an email", w.PASSWORD),
        "empty": ("", ""),
    }
    seen = set()
    for email, password in cases.values():
        client = w.make_client(app)
        response = w.sign_in(client, email, password)
        assert response.status_code == 401
        assert error_text(response.text) == BAD_LOGIN == "Email or password is incorrect."
        assert w.PROD.session not in client.cookies
        seen.add((response.status_code, without_email(response.text)))
    # Not only the message: the whole page is the same, apart from the email the user typed.
    assert len(seen) == 1


def test_five_failures_in_a_minute_for_one_email_are_followed_by_a_refusal(
    web_engine: Engine, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = w.make_user(web_engine)
    calls: list[str] = []
    real = accounts.verify_password

    def counting(engine: Engine, email: str, password: str) -> accounts.User | None:
        calls.append(email)
        return real(engine, email, password)

    monkeypatch.setattr(accounts, "verify_password", counting)
    clock = [1000.0]
    app.state.web.throttle._clock = lambda: clock[0]
    client = w.make_client(app)
    for _ in range(5):
        assert w.sign_in(client, user.email, "wrong password!").status_code == 401
    assert len(calls) == 5
    # The sixth is refused, with its own message, even with the right password, and without a
    # password check.
    refused = w.sign_in(client, user.email, w.PASSWORD)
    assert refused.status_code == 429
    assert error_text(refused.text) == THROTTLED == "Too many attempts. Try again in a minute."
    assert len(calls) == 5
    assert w.PROD.session not in client.cookies
    # The same email in other letters is the same key.
    assert w.sign_in(client, user.email.upper(), w.PASSWORD).status_code == 429
    # Another email is not affected.
    other = w.make_user(web_engine)
    assert w.sign_in(w.make_client(app), other.email).status_code == 303
    # After the minute the counter has run out.
    clock[0] += 61
    assert w.sign_in(client, user.email, w.PASSWORD).status_code == 303
    assert len(calls) == 7


def test_the_refusal_is_the_same_for_an_email_nobody_has(
    web_engine: Engine, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = w.make_user(web_engine)
    unknown = f"nobody-{secrets.token_hex(3)}@demo.example"
    outcomes: dict[str, list[tuple[int, str]]] = {}
    for label, email in (("known", known.email), ("unknown", unknown)):
        client = w.make_client(app)
        outcomes[label] = []
        for _ in range(7):
            response = w.sign_in(client, email, "wrong password!")
            outcomes[label].append((response.status_code, error_text(response.text)))
    assert outcomes["known"] == outcomes["unknown"]
    assert [status for status, _ in outcomes["known"]] == [401] * 5 + [429] * 2


def test_a_success_does_not_clear_the_counter_of_another_email(
    web_engine: Engine, app: Any
) -> None:
    victim = w.make_user(web_engine)
    mine = w.make_user(web_engine)
    attacker = w.make_client(app)
    for _ in range(5):
        w.sign_in(attacker, victim.email, "wrong password!")
    assert w.sign_in(w.make_client(app), mine.email).status_code == 303
    assert w.sign_in(attacker, victim.email, w.PASSWORD).status_code == 429


def test_neither_the_password_nor_the_typed_email_is_logged(
    web_engine: Engine, app: Any, caplog: pytest.LogCaptureFixture
) -> None:
    user = w.make_user(web_engine)
    typed = f"typed-{secrets.token_hex(4)}@demo.example"
    secret = secrets.token_urlsafe(20)
    caplog.set_level(logging.DEBUG)
    client = w.make_client(app)
    w.sign_in(client, typed, secret)
    w.sign_in(client, user.email, secret)
    w.sign_in(client, user.email, w.PASSWORD)
    for text in (typed, secret, w.PASSWORD, user.email):
        assert text not in caplog.text


# --- `next`: no open redirect ---


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("/admin/users", "/admin/users"),
        ("/account?done=revoked", "/account?done=revoked"),
        ("", "/"),
        ("//evil.example", "/"),
        ("//evil.example/path", "/"),
        ("/\\evil.example", "/"),
        ("\\\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("http://evil.example/x", "/"),
        ("javascript:alert(1)", "/"),
        ("evil.example", "/"),
        ("/\t/evil.example", "/"),
        ("/\r\nSet-Cookie: x=y", "/"),
        (" //evil.example", "/"),
        ("/account\\..\\evil", "/"),
        ("/" + "a" * 600, "/"),
    ],
)
def test_signing_in_never_redirects_to_another_site(
    web_engine: Engine, app: Any, given: str, expected: str
) -> None:
    user = w.make_user(web_engine)
    client = w.make_client(app)
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": w.csrf_in(page.text),
            "email": user.email,
            "password": w.PASSWORD,
            "next": given,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == expected


def test_a_next_in_the_address_of_the_form_is_checked_too(app: Any) -> None:
    client = w.make_client(app)
    page = client.get("/login", params={"next": "//evil.example"})
    assert 'name="next" value="/"' in page.text
    page = client.get("/login", params={"next": "/admin/users"})
    assert 'name="next" value="/admin/users"' in page.text


def test_a_signed_out_request_returns_to_its_page_after_sign_in(
    web_engine: Engine, app: Any
) -> None:
    admin = w.make_user(web_engine, "admin")
    client = w.make_client(app)
    redirect = client.get("/admin/users")
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/login?next=%2Fadmin%2Fusers"
    page = client.get(redirect.headers["location"])
    response = client.post(
        "/login",
        data={
            "csrf_token": w.csrf_in(page.text),
            "email": admin.email,
            "password": w.PASSWORD,
            "next": "/admin/users",
        },
    )
    assert response.headers["location"] == "/admin/users"


# --- the session ---


def test_signing_in_issues_a_new_session_whatever_cookie_came_with_it(
    web_engine: Engine, app: Any
) -> None:
    user = w.make_user(web_engine)
    state = app.state.web
    client = w.make_client(app)
    # A session the attacker prepared (valid for another user, or forged) is not reused.
    planted, session = state.codec.issue_session(user.id)
    w.plant(client, w.PROD.session, planted)
    assert w.sign_in(client, user.email).status_code == 303
    first = client.cookies[w.PROD.session]
    assert first != planted
    assert w.sign_in(client, user.email).status_code == 303
    second = client.cookies[w.PROD.session]
    assert second != first
    csrf_first = state.codec.load_session(first).csrf
    csrf_second = state.codec.load_session(second).csrf
    assert len({csrf_first, csrf_second, session.csrf}) == 3


def test_the_session_cookie_holds_the_user_id_the_time_and_the_csrf_secret_only(
    web_engine: Engine, app: Any
) -> None:
    user = w.make_user(web_engine, "admin", teams=("credit-risk",), name="Rina Wijaya")
    client = w.signed_in(app, user)
    value = client.cookies[w.PROD.session]
    payload = app.state.web.codec._session.loads(value)
    assert set(payload) == {"u", "iat", "csrf"}
    assert payload["u"] == user.id
    # The cookie is signed, not encrypted: whatever it holds, its owner can read. So it holds
    # nothing about the user but the id.
    import base64

    raw = base64.urlsafe_b64decode(value.split(".")[0] + "==").decode("utf-8", "replace")
    for text in (user.email, "admin", "credit-risk", "Rina", "password"):
        assert text not in raw


def test_a_page_never_renews_the_cookie(web_engine: Engine, app: Any) -> None:
    client = w.signed_in(app, w.make_user(web_engine))
    for path in ("/account", "/"):
        response = client.get(path)
        assert response.status_code == 200
        assert "set-cookie" not in response.headers


def test_a_forged_or_foreign_cookie_is_signed_out(web_engine: Engine, app: Any) -> None:
    user = w.make_user(web_engine)
    client = w.signed_in(app, user)
    good = client.cookies[w.PROD.session]
    other_app = create_app(w.make_settings(web_engine.url, secret_key=secrets.token_hex(32)))
    foreign, _ = other_app.state.web.codec.issue_session(user.id)
    other_app.state.services.dispose()
    payload, _, signature = good.rpartition(".")
    # The first character of the signature always carries signature bits (the last one may not).
    flipped = f"{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    altered = ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature
    for bad in (flipped, altered, foreign, "", "garbage", good.split(".")[0], "a" * 5000):
        probe = w.make_client(app)
        w.plant(probe, w.PROD.session, bad)
        response = probe.get("/account")
        assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_a_session_ends_eight_hours_after_it_was_issued(web_engine: Engine, app: Any) -> None:
    user = w.make_user(web_engine)
    state = app.state.web
    now = [10_000_000.0]
    state.codec._clock = lambda: now[0]
    value, _ = state.codec.issue_session(user.id)
    client = w.make_client(app)
    w.plant(client, w.PROD.session, value)
    now[0] += 8 * 3600 - 5
    assert client.get("/account").status_code == 200
    now[0] += 10
    response = client.get("/account")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    # The refused cookie is cleared.
    assert "max-age=0" in response.headers["set-cookie"].lower()


def test_parallel_attempts_for_one_email_get_at_most_five_password_checks(
    web_engine: Engine, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check and the count are one step: a burst cannot slip past the limit."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    user = w.make_user(web_engine)
    checks: list[str] = []
    real = accounts.verify_password

    def counting(engine: Engine, email: str, password: str) -> accounts.User | None:
        checks.append(email)
        return real(engine, email, password)

    monkeypatch.setattr(accounts, "verify_password", counting)
    probe = w.make_client(app)
    first = probe.get("/login")
    token = w.csrf_in(first.text)
    login_cookie = probe.cookies[w.PROD.login]
    barrier = threading.Barrier(30)

    def attempt(password: str) -> int:
        client = w.make_client(app)
        w.plant(client, w.PROD.login, login_cookie)
        barrier.wait()
        response = client.post(
            "/login", data={"csrf_token": token, "email": user.email, "password": password}
        )
        return int(response.status_code)

    wrong = [secrets.token_urlsafe(12) for _ in range(30)]
    with ThreadPoolExecutor(30) as pool:
        codes = list(pool.map(attempt, wrong))
    assert sorted(set(codes)) == [401, 429]
    assert codes.count(401) == 5 and codes.count(429) == 25
    assert len(checks) == 5
    # After the limit the right password does not sign in either.
    late = w.make_client(app)
    assert w.sign_in(late, user.email, w.PASSWORD).status_code == 429
    assert w.PROD.session not in late.cookies


def test_the_refusal_page_asks_the_client_to_wait_a_minute(web_engine: Engine, app: Any) -> None:
    user = w.make_user(web_engine)
    client = w.make_client(app)
    for _ in range(5):
        w.sign_in(client, user.email, "wrong password!")
    refused = w.sign_in(client, user.email, "wrong password!")
    assert refused.status_code == 429 and refused.headers["retry-after"] == "60"


def test_an_outage_during_the_password_check_does_not_use_up_an_attempt(
    web_engine: Engine, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = w.make_user(web_engine)

    def down(self: accounts.AccountStore, email: str, password: str) -> None:
        raise accounts.AccountStoreError

    client = w.make_client(app)
    with monkeypatch.context() as patch:
        patch.setattr(accounts.AccountStore, "verify_password", down)
        for _ in range(8):
            assert w.sign_in(client, user.email).status_code == 503
    assert w.sign_in(client, user.email).status_code == 303


def test_an_absurdly_long_password_is_counted_like_any_other_attempt(
    web_engine: Engine, app: Any
) -> None:
    user = w.make_user(web_engine)
    client = w.make_client(app)
    for _ in range(5):
        assert w.sign_in(client, user.email, "x" * 5000).status_code == 401
    assert w.sign_in(client, user.email, "x" * 5000).status_code == 429
