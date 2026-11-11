"""Entry point of the `makna` command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from makna.definitions.loader import load_definitions
from makna.definitions.validate import validate


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
            "Check every definition file under a directory (definition errors V01 to V18). "
            "Prints one line per problem as 'file:line: CODE message'. "
            "Exits 1 if there is any problem, 0 otherwise."
        ),
    )
    check.add_argument("directory", type=Path, help="the definitions directory")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
