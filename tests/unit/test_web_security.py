"""WP-16, unit tier: the cookies, the `next` check, the throttle, the settings, and the
vendored files. No database and no HTTP."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from makna.config import ConfigError, load_settings
from makna.web.security import (
    LOGIN_MAX_AGE,
    SESSION_MAX_AGE,
    CookieCodec,
    LoginThrottle,
    new_csrf_secret,
    safe_next,
    tokens_match,
)

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "src" / "makna" / "web" / "static" / "vendor"


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# --- the cookies ---


def test_a_session_round_trips_and_holds_three_things() -> None:
    codec = CookieCodec("secret-one")
    value, session = codec.issue_session(42)
    assert codec.load_session(value) == session
    assert session.user_id == 42 and len(session.csrf) == 43
    payload = codec._session.loads(value)
    assert set(payload) == {"u", "iat", "csrf"}


def test_a_session_signed_with_another_key_or_another_purpose_is_refused() -> None:
    codec = CookieCodec("secret-one")
    value, _ = codec.issue_session(1)
    assert CookieCodec("secret-two").load_session(value) is None
    # The login cookie and the session cookie are not interchangeable.
    login, _ = codec.issue_login()
    assert codec.load_session(login) is None
    assert codec.load_login(value) is None


def test_a_session_ends_eight_hours_after_it_was_issued_and_is_never_renewed() -> None:
    clock = Clock()
    codec = CookieCodec("k", clock)
    value, _ = codec.issue_session(1)
    clock.now += SESSION_MAX_AGE
    assert codec.load_session(value) is not None
    clock.now += 1
    assert codec.load_session(value) is None
    assert SESSION_MAX_AGE == 8 * 3600


def test_a_cookie_from_the_future_is_refused() -> None:
    clock = Clock()
    codec = CookieCodec("k", clock)
    value, _ = codec.issue_session(1)
    clock.now -= 3600
    assert codec.load_session(value) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"u": 1, "iat": 1_800_000_000, "csrf": "x" * 43, "role": "admin"},
        {"u": 1, "iat": 1_800_000_000},
        {"u": "1", "iat": 1_800_000_000, "csrf": "x" * 43},
        {"u": True, "iat": 1_800_000_000, "csrf": "x" * 43},
        {"u": 0, "iat": 1_800_000_000, "csrf": "x" * 43},
        {"u": -3, "iat": 1_800_000_000, "csrf": "x" * 43},
        {"u": 1, "iat": "1800000000", "csrf": "x" * 43},
        {"u": 1, "iat": 1_800_000_000.5, "csrf": "x" * 43},
        {"u": 1, "iat": 1_800_000_000, "csrf": "short"},
        {"u": 1, "iat": 1_800_000_000, "csrf": 7},
        [1, 2, 3],
        "text",
    ],
)
def test_a_correctly_signed_cookie_of_the_wrong_shape_is_refused(payload: object) -> None:
    clock = Clock()
    codec = CookieCodec("k", clock)
    # Sign it the way the codec does (same derived key and salt), to reach the shape check.
    value = codec._session.dumps(payload)
    assert codec.load_session(value) is None


def test_garbage_is_not_a_session() -> None:
    codec = CookieCodec("k")
    for value in (None, "", "x", "a.b", "a" * 5000, "e30.x", "é"):
        assert codec.load_session(value) is None
        assert codec.load_login(value) is None


def test_the_login_cookie_lives_fifteen_minutes() -> None:
    clock = Clock()
    codec = CookieCodec("k", clock)
    value, secret = codec.issue_login()
    clock.now += LOGIN_MAX_AGE
    assert codec.load_login(value) == secret
    clock.now += 1
    assert codec.load_login(value) is None


def test_csrf_secrets_are_random_and_compared_exactly() -> None:
    first, second = new_csrf_secret(), new_csrf_secret()
    assert first != second and len(first) == 43
    assert tokens_match(first, first)
    assert not tokens_match(first, second)
    assert not tokens_match("", "")
    assert not tokens_match(first, "")
    assert not tokens_match("", first)
    assert not tokens_match(first + " ", first)


# --- `next` ---


@pytest.mark.parametrize(
    "value",
    ["/", "/account", "/admin/users?done=created", "/metrics/npf_ratio#x", "/search?q=a%20b"],
)
def test_local_paths_are_kept(value: str) -> None:
    assert safe_next(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "",
        "account",
        "//evil.example",
        "///evil.example",
        "/\\evil.example",
        "\\evil.example",
        "http://evil.example",
        "https://evil.example/a",
        "HTTP://evil.example",
        "javascript:alert(1)",
        "data:text/html,x",
        "/\t/evil.example",
        "/\n/evil.example",
        "/%0d%0a" + "\r\nX: y",
        " /account",
        "/café",
        "/" + "a" * 512,
    ],
)
def test_anything_else_becomes_the_start_page(value: object) -> None:
    assert safe_next(value) == "/"


# --- the throttle ---


def test_five_failures_in_a_minute_refuse_until_the_minute_has_passed() -> None:
    clock = Clock(0.0)
    throttle = LoginThrottle(clock=clock)
    for _ in range(4):
        throttle.record_failure("a@demo.example")
        assert not throttle.refused("a@demo.example")
    throttle.record_failure("a@demo.example")
    assert throttle.refused("a@demo.example")
    assert throttle.refused("  A@Demo.Example ")  # the same email in other letters
    assert not throttle.refused("b@demo.example")
    clock.now = 59.9
    assert throttle.refused("a@demo.example")
    clock.now = 60.0
    assert not throttle.refused("a@demo.example")


def test_failures_spread_over_more_than_a_minute_do_not_add_up() -> None:
    clock = Clock(0.0)
    throttle = LoginThrottle(clock=clock)
    for _ in range(20):
        throttle.record_failure("a@demo.example")
        clock.now += 20  # three failures in a minute, never five
        assert not throttle.refused("a@demo.example")


def test_the_throttle_keeps_a_bounded_number_of_emails_and_stores_no_text() -> None:
    throttle = LoginThrottle(max_keys=100)
    for index in range(1000):
        throttle.record_failure(f"user{index}@demo.example")
    assert len(throttle._failures) == 100
    assert all(re.fullmatch(r"[0-9a-f]{64}", key) for key in throttle._failures)
    assert not throttle.refused("user0@demo.example")  # the oldest were dropped


def test_a_very_long_email_is_one_bounded_key() -> None:
    throttle = LoginThrottle()
    huge = "a" * 10_000_000 + "@x"
    for _ in range(5):
        throttle.record_failure(huge)
    assert throttle.refused(huge)
    assert throttle.key(huge) == hashlib.sha256(("a" * 1000).encode()).hexdigest()


# --- the settings ---

ENV = {"MAKNA_DATABASE_URL": "postgresql://u:p@localhost/x", "MAKNA_SECRET_KEY": "k"}


def test_development_mode_is_off_unless_set(tmp_path: Path) -> None:
    assert load_settings(ENV, tmp_path / "none.toml").web.development is False
    assert load_settings(ENV, ROOT / "config" / "makna.toml").web.development is False


def test_development_mode_comes_from_the_file_and_the_environment_overrides_it(
    tmp_path: Path,
) -> None:
    config = tmp_path / "makna.toml"
    config.write_text("[web]\ndevelopment = true\n")
    assert load_settings(ENV, config).web.development is True
    assert load_settings({**ENV, "MAKNA_WEB_DEVELOPMENT": "false"}, config).web.development is False
    assert (
        load_settings(
            {**ENV, "MAKNA_WEB_DEVELOPMENT": "TRUE"}, tmp_path / "none.toml"
        ).web.development
        is True
    )


@pytest.mark.parametrize("value", ["yes", "1", "ture", "on"])
def test_a_development_value_that_is_not_true_or_false_is_an_error(
    value: str, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match="MAKNA_WEB_DEVELOPMENT"):
        load_settings({**ENV, "MAKNA_WEB_DEVELOPMENT": value}, tmp_path / "none.toml")


def test_the_development_stack_sets_development_mode() -> None:
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text()
    assert 'MAKNA_WEB_DEVELOPMENT: "true"' in compose
    assert "MAKNA_WEB_DEVELOPMENT" in (ROOT / "deploy" / ".env.example").read_text()


# --- the vendored files ---


def test_each_vendored_file_has_the_hash_and_licence_recorded_in_vendored_md() -> None:
    text = (VENDOR / "VENDORED.md").read_text()
    rows = re.findall(r"^`([^`]+)` \| `[^`]+` \| `([0-9a-f]{64})`$", text, re.M)
    listed = {path for path, _ in rows}
    on_disk = {str(path.relative_to(VENDOR)) for path in VENDOR.rglob("*") if path.is_file()} - {
        "VENDORED.md"
    }
    assert listed == on_disk
    for path, digest in rows:
        assert hashlib.sha256((VENDOR / path).read_bytes()).hexdigest() == digest, path
    for licence in ("htmx/LICENSE", "pico/LICENSE.md", "ace/LICENSE"):
        assert licence in listed
    for name in ("0BSD", "MIT", "BSD-3-Clause"):
        assert name in text
