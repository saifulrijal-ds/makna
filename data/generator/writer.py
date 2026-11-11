"""Arrow conversion and byte-stable Parquet output for the table specification."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .spec import TABLES, ColumnSpec, TableSpec

ARROW_TYPES: Mapping[str, pa.DataType] = {
    "varchar": pa.string(),
    "integer": pa.int32(),
    "decimal(18,2)": pa.decimal128(18, 2),
    "decimal(5,2)": pa.decimal128(5, 2),
    "date": pa.date32(),
    "timestamp": pa.timestamp("us"),
    "boolean": pa.bool_(),
}

# Fixed writer options: identical input gives identical bytes.
PARQUET_OPTIONS: Mapping[str, object] = {
    "compression": "zstd",
    "compression_level": 3,
    "use_dictionary": True,
    "write_statistics": True,
    "data_page_version": "2.0",
    "version": "2.6",
    "row_group_size": 100_000,
}


def arrow_schema(spec: TableSpec) -> pa.Schema:
    """Arrow schema of a table: one field per column, in specification order."""
    return pa.schema([pa.field(c.name, ARROW_TYPES[c.sql_type]) for c in spec.columns])


def _hundredths(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise TypeError(f"decimal columns take integers in hundredths, got {value!r}")
    return Decimal(value).scaleb(-2)


def _column_array(column: ColumnSpec, values: Sequence[object]) -> pa.Array:
    """Build one Arrow array.

    Integers for date columns are days since 1970-01-01, for timestamp columns seconds since
    the epoch, and for decimal columns the value multiplied by 100.
    """
    sql_type = column.sql_type
    if sql_type == "date":
        return pa.array(values, type=pa.int32()).cast(pa.date32())
    if sql_type == "timestamp":
        return pc.multiply(pa.array(values, type=pa.int64()), 1_000_000).cast(pa.timestamp("us"))
    if sql_type.startswith("decimal"):
        dec = ARROW_TYPES[sql_type]
        return pa.array([_hundredths(v) for v in values], type=dec)
    return pa.array(values, type=ARROW_TYPES[sql_type])


def to_arrow(spec: TableSpec, columns: Mapping[str, Sequence[object]]) -> pa.Table:
    """Convert generated column lists to an Arrow table that has exactly the specified columns."""
    if set(columns) != set(spec.column_names):
        raise ValueError(
            f"{spec.name}: generated columns {sorted(columns)} differ from the "
            f"specification {sorted(spec.column_names)}"
        )
    arrays = [_column_array(c, columns[c.name]) for c in spec.columns]
    return pa.Table.from_arrays(arrays, schema=arrow_schema(spec))


def write_parquet(tables: Mapping[str, pa.Table], out_dir: Path) -> dict[str, Path]:
    """Write one `<table>.parquet` per table into `out_dir` and return the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for spec in TABLES:
        path = out_dir / f"{spec.name}.parquet"
        # Write beside the target and rename, so a crash never leaves a truncated file.
        temp = out_dir / f".{spec.name}.parquet.tmp"
        pq.write_table(tables[spec.name], temp, **PARQUET_OPTIONS)
        os.replace(temp, path)
        paths[spec.name] = path
    return paths


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
