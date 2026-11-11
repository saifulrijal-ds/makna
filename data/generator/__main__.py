"""Command line: write the sample data as Parquet files."""

from __future__ import annotations

import argparse
from pathlib import Path

from .build import DEFAULT_SEED, generate
from .writer import write_parquet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m data.generator", description="Generate the synthetic lending data."
    )
    parser.add_argument("--out", type=Path, default=Path("data/generated"), help="output directory")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed")
    parser.add_argument("--scale", type=float, default=1.0, help="size factor (1.0 = full size)")
    args = parser.parse_args(argv)
    tables = generate(args.seed, args.scale)
    paths = write_parquet(tables, args.out)
    for name, path in paths.items():
        print(f"{name}: {tables[name].num_rows} rows -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
