"""Where the integration and engine tiers find their PostgreSQL, and what a missing one means."""

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


ENGINE_URL_VARIABLE = "MAKNA_ENGINE_URL"
REQUIRE_ENGINE_VARIABLE = "MAKNA_REQUIRE_ENGINE_DATABASE"
ENGINE_SKIP_REASON = (
    f"{ENGINE_URL_VARIABLE} is not set (needs PostgreSQL holding the generated sample data)"
)


def find_engine_url(env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Where the engine tier finds the sample data: the `lending` database as role
    `lending_reader`, as the app reads it. Same contract as `find_test_database_url`: with no URL,
    (None, reason), and a reason starting with "FAIL" when `MAKNA_REQUIRE_ENGINE_DATABASE` is set
    (CI), so a missing database there is a failure and not a silent skip."""
    url = env.get(ENGINE_URL_VARIABLE)
    if url:
        return url, None
    if env.get(REQUIRE_ENGINE_VARIABLE):
        return None, f"FAIL: {REQUIRE_ENGINE_VARIABLE} is set but {ENGINE_URL_VARIABLE} is not"
    return None, ENGINE_SKIP_REASON
