"""What the evals run against: the application database, the embedding provider, the settings the
product itself runs with, and the read-only engine connection.

Settings come from the same places the product reads them (`config/makna.toml` and the
`MAKNA_*` variables, through `makna.config`), so an eval measures the configuration that is
deployed. `MAKNA_SECRET_KEY` is not needed: the evals start no server.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Engine, text

from makna.config import (
    ConfigError,
    load_engine_runtime,
    load_governance_runtime,
    load_search_runtime,
)
from makna.index.db import database_url_from_env, make_engine
from makna.model.governance import GovernancePolicy
from makna.model.search import EmbeddingProvider, SearchSettings
from makna.search.embedding import EmbeddingError, make_provider

TODAY = "2026-07-01"
"""docs/EVALS.md section 1: every eval treats this as the current date; none reads the clock."""

REPOSITORY = Path(__file__).resolve().parent.parent


class EvalSetupError(Exception):
    """The evals cannot start (a missing variable, a database or model that cannot be reached).
    The message says what to check. The command exits 2."""


@dataclass
class Runtime:
    engine: Engine
    provider: EmbeddingProvider
    provider_name: str
    search: SearchSettings
    policy: GovernancePolicy
    dialect: Literal["postgres", "duckdb"]
    engine_url: str | None
    statement_timeout_seconds: int

    def dispose(self) -> None:
        self.engine.dispose()


def open_runtime() -> Runtime:
    """Read the settings and build the engine and the embedding provider. Nothing is queried
    yet except what building them needs."""
    try:
        search_runtime = load_search_runtime()
        governance = load_governance_runtime()
        engine_runtime = load_engine_runtime()
        key = (
            search_runtime.embedding_api_key.get_secret_value()
            if search_runtime.embedding_api_key
            else None
        )
        provider = make_provider(search_runtime.embedding_provider, search_runtime.embedding, key)
        database_url = database_url_from_env()
    except (ConfigError, EmbeddingError, RuntimeError) as error:
        raise EvalSetupError(str(error)) from None
    dialect = governance.dialect
    if dialect not in ("postgres", "duckdb"):
        raise EvalSetupError(f"dialect must be postgres or duckdb, not {dialect!r}")
    return Runtime(
        engine=make_engine(database_url),
        provider=provider,
        provider_name=search_runtime.embedding_provider,
        search=search_runtime.search,
        policy=governance.governance.policy(),
        dialect="duckdb" if dialect == "duckdb" else "postgres",
        engine_url=(
            engine_runtime.engine_url.get_secret_value() if engine_runtime.engine_url else None
        ),
        statement_timeout_seconds=engine_runtime.engine.statement_timeout_seconds,
    )


def require_engine_url(runtime: Runtime) -> str:
    if not runtime.engine_url:
        raise EvalSetupError(
            "MAKNA_ENGINE_URL is not set: the compile evals run the compiled SQL on the sample "
            "database with the read-only role (the lending database as lending_reader)"
        )
    return runtime.engine_url


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def git_state() -> dict[str, Any]:
    """The commit under test. Without git (the app image has none), `GITHUB_SHA` or
    `MAKNA_GIT_COMMIT` from the environment; otherwise unknown."""
    commit = _git("rev-parse", "HEAD") or os.environ.get("GITHUB_SHA")
    commit = commit or os.environ.get("MAKNA_GIT_COMMIT")
    status = _git("status", "--porcelain", "--", ".")
    return {"commit": commit or "unknown", "dirty": None if status is None else bool(status)}


def index_state(runtime: Runtime) -> dict[str, Any]:
    """The index the evals ran against: its model, definitions version, and size."""
    with runtime.engine.connect() as connection:
        state = {
            row.key: row.value
            for row in connection.execute(
                text(
                    "SELECT key, value FROM index_state "
                    "WHERE key IN ('embedding_model', 'definitions_version')"
                )
            )
        }
        documents = connection.execute(text("SELECT count(*) FROM search_document")).scalar_one()
    return {
        "embedding_model": state.get("embedding_model"),
        "definitions_version": state.get("definitions_version"),
        "documents": int(documents),
    }


def describe_settings(runtime: Runtime) -> dict[str, Any]:
    """Everything that can change a score, so two reports can be compared."""
    search = runtime.search
    return {
        "search": {
            "w_keyword": search.w_keyword,
            "w_vector": search.w_vector,
            "candidates": search.candidates,
            "confidence": search.confidence.model_dump(),
            "status_weights": dict(search.status_weights),
            "kind_weights": dict(search.kind_weights),
        },
        "embedding": {
            "provider": runtime.provider_name,
            "model": runtime.provider.model_id,
            "dimension": runtime.provider.dimension,
        },
        "governance": {
            "mode": runtime.policy.mode,
            "min_group_size": runtime.policy.min_group_size,
            "fingerprint": runtime.policy.fingerprint(),
        },
        "dialect": runtime.dialect,
        "today": TODAY,
        "index": index_state(runtime),
        "git": git_state(),
    }
