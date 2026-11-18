"""The `init` runner of the development stack: an ordered list of idempotent steps.

Run it as `python -m deploy.init` (the `init` service of docker-compose.yml does). It lives
outside `src/makna` because the first steps use the sample-data code in `data/`, which the
installed product package must not import. Later work packages append their step to `STEPS`
(definitions repository). A step is safe to run again:
the second run of the whole list changes nothing and exits 0.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.exc import SQLAlchemyError

from data.generator import DEFAULT_SEED, TABLES, generate, write_parquet
from data.lending_loader import load_all
from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.config import load_governance_runtime, load_search_runtime, load_tag_map
from makna.definitions.loader import definition_objects, load_definitions, official_objects
from makna.definitions.repo import GitRepo, is_repository_root
from makna.index import indexer, migrate
from makna.index.db import make_engine
from makna.search.embedding import make_provider
from makna.services import accounts, store
from makna.services.derive import make_derive
from makna.services.examples import validate_for_build
from makna.services.lifecycle import seed_repository


class InitError(Exception):
    """A step cannot run; the message says what is missing or wrong."""


@dataclass(frozen=True)
class Context:
    data_dir: Path
    seed: int
    scale: float
    database_url: str
    """Application database (`MAKNA_DATABASE_URL`), as the `makna` role."""
    lending_loader_url: str
    """`lending` database as the loader role (`MAKNA_LENDING_LOADER_URL`)."""
    catalog_file: Path = Path("data/catalog/lending.catalog.yaml")
    """The catalog export that `sync-catalog` reads."""
    definitions_dir: Path = Path("semantic/definitions")
    """The sample definitions of the source tree: the content that `definitions-repo` seeds the
    repository with, and what the reference check and the index read while there is no
    repository of their own (`definitions_repo` unset). Never written to."""
    definitions_repo: Path | None = None
    """`MAKNA_DEFINITIONS_REPO`: the definitions git repository (ARCHITECTURE section 10). When it
    is a repository of its own, the reference check and the index read it, as the application
    committed it, and not `definitions_dir`."""
    demo_password: str | None = field(default=None, repr=False)
    """`MAKNA_DEMO_PASSWORD`: the password of the demo users. Without it, no demo user is made.
    Left out of `repr` so that it cannot reach a log."""


@dataclass(frozen=True)
class Step:
    name: str
    run: Callable[[Context], str]
    """Does the step's work and returns a short outcome such as `created` or `unchanged`."""


def _unreadable(path: Path) -> bool:
    """True when the file is missing or its Parquet footer cannot be read (a crashed write)."""
    try:
        pq.ParquetFile(path).close()
    except (OSError, pa.ArrowException):
        return True
    return False


def generate_sample_data(context: Context) -> str:
    if not any(_unreadable(context.data_dir / f"{t.name}.parquet") for t in TABLES):
        return "unchanged (all nine files present)"
    write_parquet(generate(context.seed, context.scale), context.data_dir)
    return f"generated (seed {context.seed}, scale {context.scale})"


def load_lending(context: Context) -> str:
    loaded = [
        name
        for name, changed in load_all(context.lending_loader_url, context.data_dir).items()
        if changed
    ]
    return f"loaded {', '.join(loaded)}" if loaded else "unchanged"


def run_migrations(context: Context) -> str:
    before = migrate.current_revision(context.database_url)
    migrate.upgrade(context.database_url)
    after = migrate.current_revision(context.database_url)
    return "unchanged" if before == after else f"upgraded {before} -> {after}"


def _repository(context: Context) -> GitRepo | None:
    """The definitions repository, when one is configured and is a repository of its own."""
    path = context.definitions_repo
    return GitRepo(path) if path is not None and is_repository_root(path) else None


def seed_definitions_step(context: Context) -> str:
    """Make `MAKNA_DEFINITIONS_REPO` a git repository with the sample definitions in it.

    When the path is missing or empty, it is made a repository and the definitions of
    `semantic/definitions` are committed as one application action, `seed`. A repository that
    already has content, or a directory that holds other things, is left alone, so a second `init`
    changes nothing. Nothing is ever written into `semantic/definitions`. The commit is completed
    in the index by the `build-index` step that follows.
    """
    if context.definitions_repo is None:
        return "skipped (MAKNA_DEFINITIONS_REPO is not set, so the definitions stay read-only)"
    engine = make_engine(context.database_url)
    try:
        outcome = seed_repository(engine, context.definitions_repo, context.definitions_dir)
    except store.StoreError as error:
        shown = "; ".join(i.format("") for i in error.issues[:5])
        raise InitError(f"{error}: {shown}" if shown else str(error)) from None
    except SQLAlchemyError as error:
        raise InitError(f"database error ({type(error).__name__})") from None
    finally:
        engine.dispose()
    return f"seeded: {outcome.message}" if outcome.seeded else f"unchanged ({outcome.message})"


def sync_catalog_step(context: Context) -> str:
    engine = make_engine(context.database_url)
    repository = _repository(context)
    found = (
        store.official_objects(repository, engine)
        if repository is not None
        else official_objects(context.definitions_dir)
    )
    try:
        result = sync_catalog(
            engine,
            FileConnector(context.catalog_file),
            tag_map=load_tag_map(),
            entities=found.entities,
            glossary_terms=found.terms,
            unreadable_objects=found.invalid,
            record_unchanged=False,
        )
    finally:
        engine.dispose()
    summary = result.summary
    warning = "".join(f" WARNING: {w}" for w in result.warnings)
    if summary.total_changes == 0:
        return "unchanged" + warning
    return (
        f"synced (datasets +{summary.datasets.added} ~{summary.datasets.changed} "
        f"-{summary.datasets.removed}, columns +{summary.columns.added} "
        f"~{summary.columns.changed} -{summary.columns.removed}, glossary terms "
        f"+{summary.glossary_terms.added}, reference issues opened {summary.issues_opened} "
        f"closed {summary.issues_closed})" + warning
    )


def build_index_step(context: Context) -> str:
    """Index the definitions and the synced catalog with the configured embedding provider.

    The provider comes from `config/makna.toml` and `MAKNA_EMBEDDING_PROVIDER` (default
    `fastembed`, whose model the image carries). A run that finds nothing to change writes
    nothing, so a second `init` prints `unchanged`.
    """
    repository = _repository(context)
    if repository is not None:
        return _sync_repository_step(context, repository)
    loaded = load_definitions(context.definitions_dir)
    problems = validate_for_build(loaded)
    if problems:
        # The same check as `makna validate`, V19 included (C-1): nothing is indexed, and the
        # stack's `init` fails, so a definition that validation rejects is never served.
        shown = "; ".join(i.format("") for i in problems[:5])
        more = f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""
        raise InitError(
            f"the definitions have {len(problems)} problem(s) that `makna validate` reports, so "
            f"nothing was indexed: {shown}{more}"
        )
    objects = definition_objects(loaded)
    if not objects:
        raise InitError(
            f"{context.definitions_dir} holds no definition objects; nothing was indexed"
        )
    runtime = load_search_runtime()
    governance = load_governance_runtime()
    derive = make_derive(governance.governance.policy(), governance.dialect)
    key = runtime.embedding_api_key.get_secret_value() if runtime.embedding_api_key else None
    provider = make_provider(runtime.embedding_provider, runtime.embedding, key)
    engine = make_engine(context.database_url)
    try:
        result = indexer.build(engine, provider, objects, derive=derive)
    finally:
        engine.dispose()
    if result.total_changes == 0:
        return "unchanged"
    return (
        f"indexed with {provider.model_id} (documents +{result.documents_added} "
        f"~{result.documents_changed} -{result.documents_removed}, objects "
        f"+{result.objects_added} ~{result.objects_changed} -{result.objects_removed})"
    )


def _sync_repository_step(context: Context, repository: GitRepo) -> str:
    """The index of a definitions repository: `makna index sync` (it also completes the `seed`)."""
    runtime = load_search_runtime()
    governance = load_governance_runtime()
    derive = make_derive(governance.governance.policy(), governance.dialect)
    key = runtime.embedding_api_key.get_secret_value() if runtime.embedding_api_key else None
    provider = make_provider(runtime.embedding_provider, runtime.embedding, key)
    engine = make_engine(context.database_url)
    try:
        state, result = store.sync(engine, provider, repository, derive=derive)
    except store.StoreError as error:
        shown = "; ".join(i.format("") for i in error.issues[:5])
        more = f" (and {len(error.issues) - 5} more)" if len(error.issues) > 5 else ""
        raise InitError(f"{error}: {shown}{more}" if shown else str(error)) from None
    finally:
        engine.dispose()
    if result.total_changes == 0 and not state.matched and not state.dead:
        return "unchanged"
    return (
        f"indexed from the repository with {provider.model_id} (documents "
        f"+{result.documents_added} ~{result.documents_changed} -{result.documents_removed}, "
        f"objects +{result.objects_added} ~{result.objects_changed} -{result.objects_removed})"
    )


@dataclass(frozen=True)
class DemoUser:
    email: str
    display_name: str
    role: str
    teams: tuple[str, ...]


# One user per role, for the development stack only (D-20: the demo approver is in the
# governance team). `.example` is a reserved top-level domain.
DEMO_USERS: tuple[DemoUser, ...] = (
    DemoUser("viewer@demo.example", "Demo Viewer", "viewer", ("credit-risk",)),
    DemoUser("curator@demo.example", "Demo Curator", "curator", ("credit-risk",)),
    DemoUser("approver@demo.example", "Demo Approver", "approver", ("data-governance",)),
    DemoUser("admin@demo.example", "Demo Admin", "admin", ()),
)


def create_demo_users(context: Context) -> str:
    """Create the four demo users with the password of `MAKNA_DEMO_PASSWORD`.

    There is no default password: without the variable nothing is created and the step succeeds.
    A user whose email exists is left exactly as it is (password, role, and teams), and nothing
    is written for it, so a second run changes nothing.
    """
    if not context.demo_password:
        return "skipped (MAKNA_DEMO_PASSWORD is not set, so no demo user was created)"
    engine = make_engine(context.database_url)
    try:
        created = []
        for demo in DEMO_USERS:
            if accounts.get_user(engine, demo.email) is not None:
                continue
            accounts.create_user(
                engine,
                demo.email,
                demo.display_name,
                demo.role,
                demo.teams,
                context.demo_password,
            )
            created.append(demo.role)
    except SQLAlchemyError as error:
        # Not the error text: it carries the SQL parameters, which include a password hash.
        raise InitError(f"database error ({type(error).__name__})") from None
    finally:
        engine.dispose()
    return f"created {', '.join(created)}" if created else "unchanged"


# Later packages append here, in the order the steps must run.
STEPS: list[Step] = [
    Step("generate-sample-data", generate_sample_data),
    Step("load-lending", load_lending),
    Step("migrate", run_migrations),
    Step("definitions-repo", seed_definitions_step),
    Step("sync-catalog", sync_catalog_step),
    Step("build-index", build_index_step),
    Step("demo-users", create_demo_users),
]


def run_steps(steps: Sequence[Step], context: Context, out: TextIO = sys.stdout) -> int:
    """Run the steps in order. Returns 0, or 1 at the first failing step (later steps skipped)."""
    for step in steps:
        try:
            outcome = step.run(context)
        except Exception as error:
            print(f"init: {step.name}: FAILED: {error}", file=out)
            return 1
        print(f"init: {step.name}: {outcome}", file=out)
    return 0


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise InitError(f"Missing required environment variable: {name}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deploy.init", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/generated"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        context = Context(
            data_dir=args.data_dir,
            seed=args.seed,
            scale=args.scale,
            database_url=_required("MAKNA_DATABASE_URL"),
            lending_loader_url=_required("MAKNA_LENDING_LOADER_URL"),
            demo_password=os.environ.get("MAKNA_DEMO_PASSWORD") or None,
            definitions_repo=Path(repo)
            if (repo := os.environ.get("MAKNA_DEFINITIONS_REPO"))
            else None,
        )
    except InitError as error:
        print(f"init: {error}", file=sys.stderr)
        return 2
    return run_steps(STEPS, context)


if __name__ == "__main__":
    raise SystemExit(main())
