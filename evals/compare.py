"""Comparing a compiled result with an expected CSV (docs/EVALS.md section 4).

Rules of "Comparing two results":
Take the answer columns. Both results must have the same number of rows. Sort both by the
non-metric columns. Text and date cells must be equal; numeric cells must agree within a relative
tolerance of 1e-6, and currency cells within an absolute 0.005. Column order and extra columns of
the compiled result are ignored.

The function describes a difference by place and kind (row count, missing column, which cell and
whether it was text, a number, or a date), not by value, so that a report or a CI log never
repeats data. `Difference.values` holds the two cells for `--verbose`.
"""

from __future__ import annotations

import csv
import datetime
import io
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

REL_TOLERANCE = Decimal("1e-6")
CURRENCY_ABS_TOLERANCE = Decimal("0.005")


@dataclass(frozen=True)
class Table:
    """A result: column names and rows. Expected tables hold text cells; compiled ones, values."""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class Difference:
    kind: str
    """`row_count`, `missing_column`, `expected_missing_column`, `text`, `number`, `date`,
    `null`, or `bad_expected_number`."""
    where: str
    """A place without values: `rows 3 != 4`, `column x`, or `row 2 column x`."""
    values: str = ""
    """The two cells, for verbose output only."""

    def describe(self, *, verbose: bool = False) -> str:
        text = f"{self.kind}: {self.where}"
        return f"{text} ({self.values})" if verbose and self.values else text


def read_expected_csv(path: Path) -> Table:
    """The committed CSV of a golden question, every cell as text."""
    return parse_csv(path.read_bytes().decode("utf-8"))


def parse_csv(text: str) -> Table:
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        return Table((), ())
    return Table(tuple(rows[0]), tuple(tuple(r) for r in rows[1:]))


def _is_number(value: object) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def decimal_of(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _text(value: object) -> str:
    """A cell as text, for ordering and for text equality."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time(0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def _sort_text(value: object) -> str:
    """Ordering form: numbers are normalised so that 12 and 12.0 sort alike on both sides."""
    text = _text(value)
    number = decimal_of(text) if text else None
    return format(number.normalize(), "f") if number is not None else text


def same_cell(expected: str, got: object, *, currency: bool) -> tuple[bool, str]:
    """`(equal, kind)` of one expected text cell and one compiled value."""
    if got is None:
        return expected == "", "null"
    if _is_number(got):
        wanted = decimal_of(expected)
        if wanted is None:
            return False, "bad_expected_number"
        actual = Decimal(str(got))
        difference = abs(actual - wanted)
        if currency:
            return difference <= CURRENCY_ABS_TOLERANCE, "number"
        scale = max(abs(actual), abs(wanted))
        return difference <= REL_TOLERANCE * scale, "number"
    if isinstance(got, datetime.date):
        return _text(got) == expected, "date"
    return _text(got) == expected, "text"


def compare_results(
    expected: Table,
    got: Table,
    answer_columns: Sequence[str],
    *,
    key_columns: Collection[str],
    currency_columns: Collection[str] = (),
) -> Difference | None:
    """None when the compiled result `got` equals the `expected` one on `answer_columns`.

    `key_columns` are the answer columns that are not metric values (time and dimension columns):
    both results are sorted by them first. `currency_columns` take the absolute tolerance.
    """
    for name in answer_columns:
        if name not in expected.columns:
            return Difference("expected_missing_column", f"column {name}")
        if name not in got.columns:
            return Difference("missing_column", f"column {name}")
    if len(expected.rows) != len(got.rows):
        return Difference("row_count", f"rows {len(expected.rows)} != {len(got.rows)}")
    e_index = {name: expected.columns.index(name) for name in answer_columns}
    g_index = {name: got.columns.index(name) for name in answer_columns}
    keys = [name for name in answer_columns if name in set(key_columns)]
    rest = [name for name in answer_columns if name not in set(key_columns)]

    order = [*keys, *rest]

    def sort_rows(
        rows: Sequence[tuple[object, ...]], index: dict[str, int]
    ) -> list[tuple[object, ...]]:
        return sorted(rows, key=lambda row: tuple(_sort_text(row[index[n]]) for n in order))

    e_rows = sort_rows(expected.rows, e_index)
    g_rows = sort_rows(got.rows, g_index)
    money = set(currency_columns)
    for position, (e_row, g_row) in enumerate(zip(e_rows, g_rows, strict=True), start=1):
        for name in answer_columns:
            want, have = e_row[e_index[name]], g_row[g_index[name]]
            equal, kind = same_cell(str(want), have, currency=name in money)
            if not equal:
                return Difference(
                    kind, f"row {position} column {name}", f"expected {want!r}, got {have!r}"
                )
    return None
