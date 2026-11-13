"""Entry point of the `makna` command."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from makna.catalog.base import ConnectorError
from makna.catalog.file import FileConnector
from makna.catalog.references import load_view, open_issues
from makna.catalog.sync import sync_catalog
from makna.config import (
    ConfigError,
    load_engine_runtime,
    load_governance_runtime,
    load_search_runtime,
    load_tag_map,
)
from makna.definitions.loader import definition_objects, load_definitions, official_objects
from makna.definitions.validate import validate
from makna.engine.base import EngineError
from makna.engine.compare import catalog_schemas, compare
from makna.engine.postgres import PostgresConnector
from makna.index import indexer
from makna.index.db import database_url_from_env, make_engine
from makna.model.definitions import Entity, GlossaryTerm
from makna.model.search import DOCUMENT_KINDS
from makna.search.embedding import EmbeddingError, make_provider
from makna.search.hybrid import ROLES, HybridSearch, SearchError
from makna.services.derive import make_derive
from makna.services.examples import check_example_requests

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
            "Check every definition file under a directory (definition errors V01 to V20). "
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
        help="sync the catalog copy, list reference issues, compare with the engine",
        description="Catalog commands. All need MAKNA_DATABASE_URL in the environment.",
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
    catalog_commands.add_parser(
        "compare",
        help="compare the catalog copy with the tables and columns of the engine",
        description=(
            "Read the synced catalog copy (MAKNA_DATABASE_URL) and the engine's tables "
            "(MAKNA_ENGINE_URL, a read-only account), and print one line per difference: "
            "'missing_table|missing_column schema.table[.column]: in the catalog, not in the "
            "engine' or 'extra_table|extra_column ...: in the engine, not in the catalog'. "
            "Only names are compared, in the schemas the catalog has tables in. Prints which "
            "engine database it read, without the password. Exits 0 if there are no "
            "differences, 1 if there are differences, 2 if a setting is missing or a database "
            "cannot be read."
        ),
    )
    index = commands.add_parser(
        "index",
        help="build the search index",
        description="Index commands. Need MAKNA_DATABASE_URL in the environment.",
    )
    index_commands = index.add_subparsers(dest="index_command", metavar="<command>")
    build = index_commands.add_parser(
        "build",
        help="index the definitions and the synced catalog for search",
        description=(
            "Turn the definition files and the catalog copy into search documents with "
            "embeddings. Only differences are written; a run that finds none writes nothing. "
            "If the configured embedding model differs from the one that built the index, "
            "every document is embedded again. Exits 0 on success, 1 if a definition file "
            "cannot be read or the index cannot be written, 2 on bad usage or settings."
        ),
    )
    build.add_argument(
        "--definitions",
        type=Path,
        default=None,
        help=(
            f"definitions directory (default: MAKNA_DEFINITIONS_REPO, else {DEFAULT_DEFINITIONS})"
        ),
    )
    search = commands.add_parser(
        "search",
        help="search the index",
        description=(
            "Hybrid search (keyword and vector) over the index. Needs MAKNA_DATABASE_URL. "
            "Prints the confidence band, then one block per hit."
        ),
    )
    search.add_argument("text", help="the query, in English or Indonesian")
    search.add_argument(
        "--role",
        choices=ROLES,
        default="viewer",
        help="who is asking: agents and viewers see approved objects only (default: viewer)",
    )
    search.add_argument(
        "--kind", action="append", choices=DOCUMENT_KINDS, help="limit to a kind (repeatable)"
    )
    search.add_argument("--domain", help="limit to a domain")
    search.add_argument(
        "--include-deprecated", action="store_true", help="also return deprecated objects"
    )
    search.add_argument("--limit", type=int, default=5, help="number of hits (default: 5)")
    return parser


def _validate(directory: Path) -> int:
    if not directory.is_dir():
        print(f"makna validate: {directory} is not a directory", file=sys.stderr)
        return 2
    issues = validate(load_definitions(directory), extra_checks=[check_example_requests])
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


def _catalog_compare() -> int:
    from sqlalchemy.exc import SQLAlchemyError

    try:
        runtime = load_engine_runtime()
        if runtime.engine_url is None:
            print(
                "makna catalog compare: Missing environment variable: MAKNA_ENGINE_URL",
                file=sys.stderr,
            )
            return 2
        engine = make_engine(database_url_from_env())
    except (RuntimeError, ConfigError) as error:
        print(f"makna catalog compare: {error}", file=sys.stderr)
        return 2
    connector = PostgresConnector(
        runtime.engine_url.get_secret_value(),
        statement_timeout_seconds=runtime.engine.statement_timeout_seconds,
    )
    try:
        with engine.connect() as connection:
            catalog = {fqn: list(columns) for fqn, columns in load_view(connection).tables.items()}
        if not catalog:
            print(
                "makna catalog compare: the catalog copy has no tables; "
                "run `makna catalog sync` first",
                file=sys.stderr,
            )
            return 2
        engine_tables = [
            table for schema in catalog_schemas(catalog) for table in connector.list_tables(schema)
        ]
    except SQLAlchemyError as error:
        print(f"makna catalog compare: cannot read the catalog copy: {error}", file=sys.stderr)
        return 2
    except EngineError as error:
        print(f"makna catalog compare: {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    differences = compare(catalog, engine_tables)
    print(f"engine: {connector.target}")
    print(f"catalog tables: {len(catalog)}, engine tables read: {len(engine_tables)}")
    for difference in differences:
        side = (
            "in the catalog, not in the engine"
            if difference.kind.startswith("missing")
            else "in the engine, not in the catalog"
        )
        print(f"{difference.kind} {difference.name}: {side}")
    if not differences:
        print("no differences")
    return 1 if differences else 0


def _index_build(definitions: Path | None) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    try:
        directory = _definitions_directory(definitions)
        if directory is None or not directory.is_dir():
            print(f"makna index build: no definitions directory ({directory})", file=sys.stderr)
            return 2
        runtime = load_search_runtime()
        governance = load_governance_runtime()
        derive = make_derive(governance.governance.policy(), governance.dialect)
        key = runtime.embedding_api_key.get_secret_value() if runtime.embedding_api_key else None
        provider = make_provider(runtime.embedding_provider, runtime.embedding, key)
        engine = make_engine(database_url_from_env())
    except (RuntimeError, ConfigError) as error:
        print(f"makna index build: {error}", file=sys.stderr)
        return 2
    loaded = load_definitions(directory)
    if loaded.load_issues:
        for issue in loaded.load_issues:
            print(issue.format(f"{directory.as_posix().rstrip('/')}/"), file=sys.stderr)
        print(
            "makna index build: not indexing, because definition files cannot be read "
            "(run `makna validate`)",
            file=sys.stderr,
        )
        return 1
    objects = definition_objects(loaded)
    if not objects:
        print(
            f"makna index build: {directory} holds no definition objects; not indexing, "
            "so that the index is not emptied by a wrong directory",
            file=sys.stderr,
        )
        engine.dispose()
        return 1
    try:
        result = indexer.build(engine, provider, objects, derive=derive)
    except (EmbeddingError, indexer.IndexerError, SQLAlchemyError) as error:
        print(f"makna index build: {error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(
        f"embedding model: {provider.model_id}"
        + (" (re-embedded all)" if result.reembedded_everything else "")
    )
    print(
        "documents: "
        f"added {result.documents_added}, changed {result.documents_changed}, "
        f"removed {result.documents_removed}, unchanged {result.documents_unchanged}"
    )
    print(
        "objects: "
        f"added {result.objects_added}, changed {result.objects_changed}, "
        f"removed {result.objects_removed}, unchanged {result.objects_unchanged}"
    )
    print("in the index: " + ", ".join(f"{k} {v}" for k, v in sorted(result.by_kind.items())))
    print(
        f"edges: added {result.edges_added}, removed {result.edges_removed}; "
        f"reference issues R05: opened {result.issues_opened}, closed {result.issues_closed}"
    )
    return 0


def _search(
    text: str,
    role: str,
    kinds: list[str] | None,
    domain: str | None,
    include_deprecated: bool,
    limit: int,
) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    try:
        runtime = load_search_runtime()
        key = runtime.embedding_api_key.get_secret_value() if runtime.embedding_api_key else None
        provider = make_provider(runtime.embedding_provider, runtime.embedding, key)
        engine = make_engine(database_url_from_env())
    except (RuntimeError, ConfigError) as error:
        print(f"makna search: {error}", file=sys.stderr)
        return 2
    try:
        result = HybridSearch(engine, provider, runtime.search).search(
            text,
            role=role,
            kinds=kinds,
            domain=domain,
            include_deprecated=include_deprecated,
            limit=limit,
        )
    except (SearchError, EmbeddingError, SQLAlchemyError) as error:
        print(f"makna search: {error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    confidence = result.confidence
    print(f"confidence: {confidence.band} (coverage {confidence.coverage:.2f})")
    if confidence.guidance:
        print(confidence.guidance)
    for rank, hit in enumerate(result.hits, start=1):
        matched = f"  matched: {', '.join(hit.matched_terms)}" if hit.matched_terms else ""
        print(f"{rank}. {hit.object_id}  [{hit.status}]  score {hit.score:.4f}{matched}")
        summary = " ".join(hit.description.split())
        print(f"   {summary[:140]}{'...' if len(summary) > 140 else ''}")
    return 0


def _serve(host: str, port: int) -> int:
    import uvicorn

    from makna.config import ConfigError, load_settings
    from makna.services import build_services

    try:
        # Fail here, with a clear message, for anything the application would refuse at start.
        build_services(load_settings()).dispose()
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
        if args.catalog_command == "compare":
            return _catalog_compare()
        print("makna catalog: choose sync, issues or compare", file=sys.stderr)
        return 2
    if args.command == "index":
        if args.index_command == "build":
            return _index_build(args.definitions)
        print("makna index: choose build", file=sys.stderr)
        return 2
    if args.command == "search":
        return _search(
            args.text, args.role, args.kind, args.domain, args.include_deprecated, args.limit
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
