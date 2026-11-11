"""Helpers for the generator tests: read tables out of docs/SAMPLE_DOMAIN.md and query Parquet."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[2]
DOMAIN_DOC = ROOT / "docs" / "SAMPLE_DOMAIN.md"

TABLE_NAMES = (
    "branches",
    "products",
    "customers",
    "loan_applications",
    "contracts",
    "installment_schedules",
    "payments",
    "collection_actions",
    "contract_monthly_snapshots",
)


def _lines() -> list[str]:
    return DOMAIN_DOC.read_text(encoding="utf-8").splitlines()


def _section(heading_prefix: str) -> list[str]:
    """Lines under the heading that starts with `heading_prefix`, up to the next heading."""
    lines = _lines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading_prefix))
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        out.append(line)
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def doc_columns() -> dict[str, list[tuple[str, str]]]:
    """Section 3: table name -> [(column, sql type)] in document order."""
    out: dict[str, list[tuple[str, str]]] = {}
    for line in _lines():
        m = re.match(r"^### 3\.\d+ `(\w+)`", line)
        if m:
            name = m.group(1)
            rows = []
            for row in _section(line):
                cells = _cells(row)
                col = re.fullmatch(r"`(\w+)`", cells[0]) if row.startswith("| `") else None
                if col and len(cells) >= 3:
                    rows.append((col.group(1), cells[1]))
            out[name] = rows
    return out


def doc_relationships() -> set[tuple[str, str, str, str]]:
    """Section 3.10: (table, column, referenced table, relationship name)."""
    out: set[tuple[str, str, str, str]] = set()
    for row in _section("### 3.10"):
        if row.startswith("| `"):
            cells = [c.strip("`") for c in _cells(row)]
            out.add((cells[0], cells[1], cells[2], cells[3]))
    return out


def doc_target_sizes() -> dict[str, int]:
    """Section 2 'Target sizes' as table name -> row count."""
    line = next(line for line in _lines() if line.startswith("- Target sizes"))
    names = {
        "branches": "branches",
        "products": "products",
        "customers": "customers",
        "applications": "loan_applications",
        "contracts": "contracts",
        "schedule rows": "installment_schedules",
        "payments": "payments",
        "snapshot rows": "contract_monthly_snapshots",
        "collection actions": "collection_actions",
    }
    out = {}
    for number, label in re.findall(r"([\d,]+) ([a-z ]+?)(?:,|\.| and|$)", line):
        label = label.strip()
        if label in names:
            out[names[label]] = int(number.replace(",", ""))
    return out


def doc_catalog_exceptions() -> dict[tuple[str, str | None], str]:
    """Section 6.1: (table, column or None) -> the listed catalog description ('' for none).

    'every column of X' is expanded with the columns of X from section 3.
    """
    columns = doc_columns()
    out: dict[tuple[str, str | None], str] = {}
    for row in _section("### 6.1"):
        if not row.startswith("| ") or row.startswith("| Object") or row.startswith("|---"):
            continue
        obj, text = _cells(row)[:2]
        if text.startswith("none"):
            description = ""
        else:
            quoted = re.match(r"`([^`]*)`", text)
            assert quoted, f"no quoted description in section 6.1 row: {row!r}"
            description = quoted.group(1)
        if m := re.fullmatch(r"table `(\w+)`", obj):
            out[(m.group(1), None)] = description
        elif m := re.fullmatch(r"every column of `(\w+)`", obj):
            for col, _ in columns[m.group(1)]:
                out[(m.group(1), col)] = description
        elif m := re.fullmatch(r"`(\w+)`: (.+)", obj):
            for col in re.findall(r"`(\w+)`", m.group(2)):
                out[(m.group(1), col)] = description
        elif m := re.fullmatch(r"`(\w+)\.(\w+)`", obj):
            out[(m.group(1), m.group(2))] = description
        else:
            raise AssertionError(f"unrecognised object in section 6.1: {obj!r}")
    return out


def connect(directory: Path) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB with one view per generated Parquet file."""
    con = duckdb.connect()
    for name in TABLE_NAMES:
        path = directory / f"{name}.parquet"
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path.as_posix()}')")
    return con


def scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    row = con.execute(sql).fetchone()
    assert row is not None
    return row[0]
