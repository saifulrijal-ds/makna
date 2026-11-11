"""The `init` runner of the development stack: an ordered list of idempotent steps.

Run it as `python -m deploy.init` (the `init` service of docker-compose.yml does). It lives
outside `src/makna` because the first steps use the sample-data code in `data/`, which the
installed product package must not import. Later work packages append their step to `STEPS`
(catalog sync, index build, demo users, definitions repository). A step is safe to run again:
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

from data.generator import DEFAULT_SEED, TABLES, generate, write_parquet
from data.lending_loader import load_all
from makna.index import migrate


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


@dataclass(frozen=True)
class Step:
    name: str
    run: Callable[[Context], str]
    """Does the step's work and returns a short outcome such as `created` or `unchanged`."""


def generate_sample_data(context: Context) -> str:
    missing = [t.name for t in TABLES if not (context.data_dir / f"{t.name}.parquet").is_file()]
    if not missing:
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


# Later packages append here, in the order the steps must run.
STEPS: list[Step] = [
    Step("generate-sample-data", generate_sample_data),
    Step("load-lending", load_lending),
    Step("migrate", run_migrations),
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
