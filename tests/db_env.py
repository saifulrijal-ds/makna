"""Where the integration tier finds its PostgreSQL, and what happens when it is not there."""

from __future__ import annotations

from collections.abc import Mapping

URL_VARIABLE = "MAKNA_TEST_DATABASE_URL"
REQUIRE_VARIABLE = "MAKNA_REQUIRE_TEST_DATABASE"
SKIP_REASON = f"{URL_VARIABLE} is not set (needs PostgreSQL with pgvector)"


def find_test_database_url(env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Return (url, problem). With no URL: (None, reason). CI sets the require variable, so a
    missing URL there is a failure (problem text starts with "FAIL"), not a silent skip."""
    url = env.get(URL_VARIABLE)
    if url:
        return url, None
    if env.get(REQUIRE_VARIABLE):
        return None, f"FAIL: {REQUIRE_VARIABLE} is set but {URL_VARIABLE} is not"
    return None, SKIP_REASON
