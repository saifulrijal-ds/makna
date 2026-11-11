"""Entry point of the `makna` command."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from makna.catalog.base import ConnectorError
from makna.catalog.file import FileConnector
from makna.catalog.references import open_issues
from makna.catalog.sync import sync_catalog
from makna.config import ConfigError, load_tag_map
from makna.definitions.loader import load_definitions, official_objects
from makna.definitions.validate import validate
from makna.index.db import database_url_from_env, make_engine
from makna.model.definitions import Entity, GlossaryTerm

DEFAULT_CATALOG_FILE = Path("data/catalog/lending.catalog.yaml")
DEFAULT_DEFINITIONS = Path("semantic/definitions")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="makna",
        description="Makna: an open semantic layer platform.",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    check = commands.add_parser(
        "validate",
        help="check the definition files in a directory",
        description=(
            "Check every definition file under a directory (definition errors V01 to V18 and V20). "
            "Prints one line per problem as 'file:line: CODE message'. "
            "Exits 1 if there is any problem, 0 otherwise."
        ),
    )
    check.add_argument("directory", type=Path, help="the definitions directory")
    serve = commands.add_parser(
        "serve",
        help="run the Makna application (health check on /healthz)",
        description=(
            "Run the Makna application. Needs MAKNA_DATABASE_URL and MAKNA_SECRET_KEY "
            "in the environment."
        ),
    )
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on")
    serve.add_argument("--port", type=int, default=8000, help="port to listen on")
    catalog = commands.add_parser(
        "catalog",
        help="sync the catalog copy and list reference issues",
        description="Catalog commands. Both need MAKNA_DATABASE_URL in the environment.",
    )
    catalog_commands = catalog.add_subparsers(dest="catalog_command", metavar="<command>")
    sync = catalog_commands.add_parser(
        "sync",
        help="sync a catalog export file into the database and run the reference check",
        description=(
            "Read a makna-catalog/v1 file, write the difference into the catalog tables, and "
            "check the definitions against it (reference issues R01 to R04). Prints a summary. "
            "Exits 0 on success, 1 if the file or the database cannot be used, 2 on bad usage."
        ),
    )
    sync.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_CATALOG_FILE,
        help=f"catalog export file (default: {DEFAULT_CATALOG_FILE})",
    )
    sync.add_argument(
        "--definitions",
        type=Path,
        default=None,
        help=(
            "definitions directory for the reference check (default: MAKNA_DEFINITIONS_REPO, "
            f"else {DEFAULT_DEFINITIONS} if it exists; without one the check is skipped)"
        ),
    )
    catalog_commands.add_parser(
        "issues",
        help="list the open reference issues",
        description="Print one line per open reference issue: 'CODE object part: message'.",
    )
    return parser


def _validate(directory: Path) -> int:
    if not directory.is_dir():
        print(f"makna validate: {directory} is not a directory", file=sys.stderr)
        return 2
    issues = validate(load_definitions(directory))
    prefix = f"{directory.as_posix().rstrip('/')}/"
    for issue in issues:
        print(issue.format(prefix))
    if issues:
        print(f"{len(issues)} problem(s) found", file=sys.stderr)
        return 1
    return 0


def _definitions_directory(given: Path | None) -> Path | None:
    if given is not None:
        return given
    from_env = os.environ.get("MAKNA_DEFINITIONS_REPO")
    if from_env:
        return Path(from_env)
    return DEFAULT_DEFINITIONS if DEFAULT_DEFINITIONS.is_dir() else None


def _catalog_sync(file: Path, definitions: Path | None) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    try:
        engine = make_engine(database_url_from_env())
        tag_map = load_tag_map()
        directory = _definitions_directory(definitions)
        entities: list[Entity] | None = None
        terms: list[GlossaryTerm] = []
        unreadable: dict[str, str] = {}
        if directory is not None:
            if not directory.is_dir():
                print(f"makna catalog sync: {directory} is not a directory", file=sys.stderr)
                return 2
            found = official_objects(directory)
            entities, terms, unreadable = found.entities, found.terms, found.invalid
        else:
            print(
                "makna catalog sync: no definitions directory, reference check skipped",
                file=sys.stderr,
            )
        result = sync_catalog(
            engine,
            FileConnector(file),
            tag_map=tag_map,
            entities=entities,
            glossary_terms=terms,
            unreadable_objects=unreadable,
        )
    except (RuntimeError, ConfigError) as error:
        print(f"makna catalog sync: {error}", file=sys.stderr)
        return 2
    except (ConnectorError, SQLAlchemyError) as error:
        print(f"makna catalog sync: {error}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"makna catalog sync: warning: {warning}", file=sys.stderr)
    summary = result.summary.as_dict()
    for group in ("datasets", "columns", "glossary_terms", "reference_issues"):
        print(f"{group}: " + ", ".join(f"{k} {v}" for k, v in summary[group].items()))
    return 0


def _catalog_issues() -> int:
    from sqlalchemy.exc import SQLAlchemyError

    try:
        engine = make_engine(database_url_from_env())
        with engine.connect() as connection:
            issues = open_issues(connection)
    except RuntimeError as error:
        print(f"makna catalog issues: {error}", file=sys.stderr)
        return 2
    except SQLAlchemyError as error:
        print(f"makna catalog issues: {error}", file=sys.stderr)
        return 1
    for issue in issues:
        print(f"{issue.code} {issue.object_id} {issue.part}: {issue.message}")
    return 0


def _serve(host: str, port: int) -> int:
    import uvicorn

    from makna.config import ConfigError, load_settings

    try:
        load_settings()
    except ConfigError as error:
        print(f"makna serve: {error}", file=sys.stderr)
        return 2
    uvicorn.run("makna.app:create_app", factory=True, host=host, port=port)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args.directory)
    if args.command == "serve":
        return _serve(args.host, args.port)
    if args.command == "catalog":
        if args.catalog_command == "sync":
            return _catalog_sync(args.file, args.definitions)
        if args.catalog_command == "issues":
            return _catalog_issues()
        print("makna catalog: choose sync or issues", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
