"""The integration tier skips locally without a database and fails in CI (opt-in switch)."""

from __future__ import annotations

from tests.db_env import (
    ENGINE_SKIP_REASON,
    ENGINE_URL_VARIABLE,
    REQUIRE_ENGINE_VARIABLE,
    REQUIRE_VARIABLE,
    SKIP_REASON,
    URL_VARIABLE,
    find_engine_url,
)
from tests.db_env import find_test_database_url as find


def test_url_present_is_returned() -> None:
    assert find({URL_VARIABLE: "postgresql://x"}) == ("postgresql://x", None)
    assert find({URL_VARIABLE: "postgresql://x", REQUIRE_VARIABLE: "1"})[1] is None


def test_missing_url_skips_when_not_required() -> None:
    assert find({}) == (None, SKIP_REASON)
    assert find({URL_VARIABLE: ""}) == (None, SKIP_REASON)


def test_missing_url_fails_when_required() -> None:
    url, problem = find({REQUIRE_VARIABLE: "1"})
    assert url is None
    assert problem is not None and problem.startswith("FAIL") and URL_VARIABLE in problem


def test_engine_url_present_is_returned() -> None:
    assert find_engine_url({ENGINE_URL_VARIABLE: "postgresql://x"}) == ("postgresql://x", None)
    both = {ENGINE_URL_VARIABLE: "postgresql://x", REQUIRE_ENGINE_VARIABLE: "1"}
    assert find_engine_url(both)[1] is None


def test_engine_url_missing_skips_unless_required() -> None:
    assert find_engine_url({}) == (None, ENGINE_SKIP_REASON)
    assert find_engine_url({ENGINE_URL_VARIABLE: ""}) == (None, ENGINE_SKIP_REASON)
    # The integration switch does not turn a missing engine database into a failure.
    assert find_engine_url({REQUIRE_VARIABLE: "1"}) == (None, ENGINE_SKIP_REASON)


def test_engine_url_missing_fails_when_required() -> None:
    url, problem = find_engine_url({REQUIRE_ENGINE_VARIABLE: "1"})
    assert url is None
    assert problem is not None and problem.startswith("FAIL") and ENGINE_URL_VARIABLE in problem
