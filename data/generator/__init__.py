"""Synthetic lending data generator (DAT-1, DAT-2).

Run `uv run python -m data.generator --out data/generated --seed 20261111 --scale 1.0`.
"""

from .build import DEFAULT_SEED, Plan, generate, plan
from .spec import TABLES, TABLES_BY_NAME, ColumnSpec, ForeignKey, TableSpec, get_table
from .writer import arrow_schema, write_parquet

__all__ = [
    "DEFAULT_SEED",
    "TABLES",
    "TABLES_BY_NAME",
    "ColumnSpec",
    "ForeignKey",
    "Plan",
    "TableSpec",
    "arrow_schema",
    "generate",
    "get_table",
    "plan",
    "write_parquet",
]
