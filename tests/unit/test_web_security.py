"""WP-16, unit tier: the cookies, the `next` check, the throttle, the settings, and the
vendored files. No database and no HTTP."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from pathlib import Path

import pytest

from makna.config import ConfigError, load_settings
from makna.web.security import (
    LOGIN_MAX_AGE,
    SESSION_MAX_AGE,
    CookieCodec,
    CookieNames,
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


def reserve_n(throttle: LoginThrottle, email: str, n: int) -> list[int | None]:
    return [throttle.reserve(email) for _ in range(n)]


def test_the_sixth_attempt_in_a_minute_is_refused_until_the_minute_has_passed() -> None:
    clock = Clock(0.0)
    throttle = LoginThrottle(clock=clock)
    assert all(t is not None for t in reserve_n(throttle, "a@demo.example", 5))
    assert throttle.reserve("a@demo.example") is None
    assert throttle.reserve("  A@Demo.Example ") is None  # the same email in other letters
    assert throttle.reserve("b@demo.example") is not None
    clock.now = 59.9
    assert throttle.reserve("a@demo.example") is None
    clock.now = 60.0
    assert throttle.reserve("a@demo.example") is not None


def test_checking_and_counting_are_one_step_so_parallel_attempts_cannot_all_pass() -> None:
    import threading

    throttle = LoginThrottle()
    results: list[int | None] = []
    barrier = threading.Barrier(50)

    def attempt() -> None:
        barrier.wait()
        results.append(throttle.reserve("a@demo.example"))

    threads = [threading.Thread(target=attempt) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for r in results if r is not None) == 5


def test_a_success_gives_back_its_own_reservation_and_nobody_else_s() -> None:
    throttle = LoginThrottle()
    tickets = reserve_n(throttle, "a@demo.example", 5)
    assert throttle.reserve("a@demo.example") is None
    # One of the five attempts succeeded: that slot is free again, one guess and no more.
    throttle.release("a@demo.example", tickets[2] or 0)
    assert throttle.reserve("a@demo.example") is not None
    assert throttle.reserve("a@demo.example") is None
    throttle.release("a@demo.example", 999_999)  # an unknown ticket changes nothing
    assert throttle.reserve("a@demo.example") is None


def test_attempts_spread_over_more_than_a_minute_do_not_add_up() -> None:
    clock = Clock(0.0)
    throttle = LoginThrottle(clock=clock)
    for _ in range(20):
        assert throttle.reserve("a@demo.example") is not None
        clock.now += 20  # three in a minute, never six


def test_the_throttle_never_drops_a_key_that_is_inside_its_minute() -> None:
    clock = Clock(0.0)
    throttle = LoginThrottle(max_keys=100, clock=clock)
    for index in range(100):
        assert throttle.reserve(f"user{index}@demo.example") is not None
    assert len(throttle._attempts) == 100
    assert all(re.fullmatch(r"[0-9a-f]{64}", key) for key in throttle._attempts)
    # Full of live keys: a new email is refused (fail closed), and the old ones keep their count.
    assert throttle.reserve("new@demo.example") is None
    assert len(throttle._attempts) == 100
    for _ in range(4):
        assert throttle.reserve("user0@demo.example") is not None
    assert throttle.reserve("user0@demo.example") is None
    # When the minute is over the expired keys make room.
    clock.now = 61.0
    assert throttle.reserve("new@demo.example") is not None


def test_a_full_throttle_logs_one_warning_that_names_no_email(
    caplog: pytest.LogCaptureFixture,
) -> None:
    throttle = LoginThrottle(max_keys=2)
    throttle.reserve("one@demo.example")
    throttle.reserve("two@demo.example")
    with caplog.at_level("WARNING"):
        for index in range(10):
            assert throttle.reserve(f"late{index}@demo.example") is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "demo.example" not in caplog.text


def test_a_very_long_email_is_one_bounded_key() -> None:
    throttle = LoginThrottle()
    huge = "a" * 10_000_000 + "@x"
    assert all(t is not None for t in reserve_n(throttle, huge, 5))
    assert throttle.reserve(huge) is None
    assert throttle.key(huge) == hashlib.sha256(("a" * 1000).encode()).hexdigest()


def test_the_cookie_names_carry_the_host_prefix_outside_development_mode() -> None:
    assert CookieNames.for_mode(False) == CookieNames("__Host-makna_session", "__Host-makna_login")
    assert CookieNames.for_mode(True) == CookieNames("makna_session", "makna_login")


def test_the_session_repr_does_not_show_the_csrf_secret() -> None:
    _, session = CookieCodec("k" * 40).issue_session(7)
    assert session.csrf not in repr(session) and "7" in repr(session)


def test_the_cookies_are_signed_with_hmac_sha256() -> None:
    codec = CookieCodec("k" * 40)
    value, _ = codec.issue_session(7)
    signature = value.rpartition(".")[2]
    assert len(base64.urlsafe_b64decode(signature + "=")) == 32  # SHA-1 would give 20 bytes


# --- the settings ---

ENV = {
    "MAKNA_DATABASE_URL": "postgresql://u:p@localhost/x",
    "MAKNA_SECRET_KEY": secrets.token_hex(32),
}


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
