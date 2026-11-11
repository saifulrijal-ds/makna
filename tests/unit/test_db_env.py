"""The integration tier skips locally without a database and fails in CI (opt-in switch)."""

from __future__ import annotations

from tests.db_env import REQUIRE_VARIABLE, SKIP_REASON, URL_VARIABLE
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
