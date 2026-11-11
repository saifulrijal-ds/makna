"""The `init` runner of the development stack: an ordered list of idempotent steps.

Run it as `python -m deploy.init` (the `init` service of docker-compose.yml does). It lives
outside `src/makna` because the first steps use the sample-data code in `data/`, which the
installed product package must not import. Later work packages append their step to `STEPS`
(demo users, definitions repository). A step is safe to run again:
the second run of the whole list changes nothing and exits 0.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import pyarrow as pa
import pyarrow.parquet as pq

from data.generator import DEFAULT_SEED, TABLES, generate, write_parquet
from data.lending_loader import load_all
from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.config import load_search_runtime, load_tag_map
from makna.definitions.loader import definition_objects, load_definitions, official_objects
from makna.index import indexer, migrate
from makna.index.db import make_engine
from makna.search.embedding import make_provider


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
    """The definitions the reference check reads, until the definitions repository (WP-18)."""


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


def sync_catalog_step(context: Context) -> str:
    found = official_objects(context.definitions_dir)
    engine = make_engine(context.database_url)
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
    loaded = load_definitions(context.definitions_dir)
    if loaded.load_issues:
        problems = "; ".join(i.format("") for i in loaded.load_issues[:3])
        raise InitError(f"definition files cannot be read, so nothing was indexed: {problems}")
    runtime = load_search_runtime()
    key = runtime.embedding_api_key.get_secret_value() if runtime.embedding_api_key else None
    provider = make_provider(runtime.embedding_provider, runtime.embedding, key)
    engine = make_engine(context.database_url)
    try:
        result = indexer.build(engine, provider, definition_objects(loaded))
    finally:
        engine.dispose()
    if result.total_changes == 0:
        return "unchanged"
    return (
        f"indexed with {provider.model_id} (documents +{result.documents_added} "
        f"~{result.documents_changed} -{result.documents_removed}, objects "
        f"+{result.objects_added} ~{result.objects_changed} -{result.objects_removed})"
    )


# Later packages append here, in the order the steps must run.
STEPS: list[Step] = [
    Step("generate-sample-data", generate_sample_data),
    Step("load-lending", load_lending),
    Step("migrate", run_migrations),
    Step("sync-catalog", sync_catalog_step),
    Step("build-index", build_index_step),
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
        )
    except InitError as error:
        print(f"init: {error}", file=sys.stderr)
        return 2
    return run_steps(STEPS, context)


if __name__ == "__main__":
    raise SystemExit(main())
