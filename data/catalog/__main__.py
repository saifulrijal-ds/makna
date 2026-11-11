"""`python -m data.catalog`: write the sample catalog export, or check that it is current."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .build import EXPORT_PATH, render_export


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m data.catalog", description=__doc__)
    parser.add_argument("--out", type=Path, default=EXPORT_PATH, help="file to write")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the file differs from a fresh export"
    )
    args = parser.parse_args(argv)
    text = render_export()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != text:
            print(f"{args.out} is not current; run `python -m data.catalog`", file=sys.stderr)
            return 1
        return 0
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
