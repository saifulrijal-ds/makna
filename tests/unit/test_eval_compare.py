"""WP-15 acceptance 3: the comparison of a compiled result with an expected CSV
(docs/EVALS.md section 4, "Comparing two results")."""

from __future__ import annotations

import datetime
from decimal import Decimal

from evals.compare import Difference, Table, compare_results, parse_csv

EXPECTED = parse_csv(
    "region,disbursement_amount,npf_ratio\n"
    "Jawa Barat,20440000000.00,0.031\n"
    "Jawa Tengah,9829000000.00,0.025\n"
    "Bali,3000.50,0.1\n"
)
COLUMNS = ("region", "disbursement_amount", "npf_ratio")
ANSWER = ["region", "disbursement_amount", "npf_ratio"]


def got(*rows: tuple[object, ...], columns: tuple[str, ...] = COLUMNS) -> Table:
    return Table(columns, tuple(rows))


def same(
    result: Table, *, currency: tuple[str, ...] = (), answer: list[str] | None = None
) -> Difference | None:
    return compare_results(
        EXPECTED,
        result,
        answer or ANSWER,
        key_columns=["region"],
        currency_columns=currency,
    )


ROWS = (
    ("Jawa Barat", Decimal("20440000000.00"), 0.031),
    ("Jawa Tengah", Decimal("9829000000.00"), 0.025),
    ("Bali", Decimal("3000.50"), 0.1),
)


def test_equal_results_are_equal() -> None:
    assert same(got(*ROWS)) is None


def test_row_order_does_not_matter() -> None:
    assert same(got(ROWS[2], ROWS[0], ROWS[1])) is None


def test_a_number_inside_the_relative_tolerance_is_equal() -> None:
    near = (ROWS[0][0], ROWS[0][1], 0.031 * (1 + 5e-7))
    assert same(got(near, ROWS[1], ROWS[2])) is None


def test_a_number_outside_the_relative_tolerance_differs() -> None:
    far = (ROWS[0][0], ROWS[0][1], 0.031 * (1 + 5e-6))
    difference = same(got(far, ROWS[1], ROWS[2]))
    assert difference is not None and difference.kind == "number"
    assert difference.where == "row 2 column npf_ratio"  # rows are paired after sorting by region


def test_an_extra_column_in_the_compiled_result_is_ignored() -> None:
    wider = Table(
        ("npf_outstanding_principal", *COLUMNS, "outstanding_principal"),
        tuple((1, *row, 2) for row in ROWS),
    )
    assert same(wider) is None


def test_column_order_is_ignored() -> None:
    reordered = Table(
        ("npf_ratio", "region", "disbursement_amount"), tuple((r[2], r[0], r[1]) for r in ROWS)
    )
    assert same(reordered) is None


def test_a_missing_row_differs() -> None:
    difference = same(got(*ROWS[:2]))
    assert difference is not None and difference.kind == "row_count"
    assert difference.where == "rows 3 != 2"


def test_an_extra_row_differs() -> None:
    extra = got(*ROWS, ("Aceh", Decimal("1"), 0.5))
    difference = same(extra)
    assert difference is not None and difference.kind == "row_count"


def test_a_missing_answer_column_differs() -> None:
    narrow = Table(("region", "npf_ratio"), tuple((r[0], r[2]) for r in ROWS))
    difference = same(narrow)
    assert difference is not None and difference.kind == "missing_column"
    assert difference.where == "column disbursement_amount"


def test_only_the_answer_columns_are_compared() -> None:
    other = (ROWS[0][0], Decimal("1"), ROWS[0][2])  # a different amount
    assert same(got(other, ROWS[1], ROWS[2]), answer=["region", "npf_ratio"]) is None


def test_currency_uses_an_absolute_tolerance() -> None:
    inside = (ROWS[2][0], Decimal("3000.504"), ROWS[2][2])
    outside = (ROWS[2][0], Decimal("3000.51"), ROWS[2][2])
    assert same(got(ROWS[0], ROWS[1], inside), currency=("disbursement_amount",)) is None
    difference = same(got(ROWS[0], ROWS[1], outside), currency=("disbursement_amount",))
    assert difference is not None and difference.kind == "number"


def test_text_must_be_equal() -> None:
    difference = same(got(("jawa barat", *ROWS[0][1:]), ROWS[1], ROWS[2]))
    # The key differs, so after sorting the rows no longer pair up.
    assert difference is not None and difference.kind == "text"


def test_dates_compare_as_iso_text() -> None:
    table = parse_csv("metric_time,value\n2026-01-01,5\n2026-02-01,6\n")
    columns = ("metric_time", "value")
    rows = ((datetime.date(2026, 2, 1), 6), (datetime.date(2026, 1, 1), 5))
    assert (
        compare_results(table, Table(columns, rows), columns, key_columns=["metric_time"]) is None
    )
    stamps = (
        (datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC), 5),
        (datetime.datetime(2026, 2, 1), 6),
    )
    assert (
        compare_results(table, Table(columns, stamps), columns, key_columns=["metric_time"]) is None
    )
    wrong = ((datetime.date(2026, 1, 2), 5), (datetime.date(2026, 2, 1), 6))
    difference = compare_results(table, Table(columns, wrong), columns, key_columns=["metric_time"])
    assert difference is not None and difference.kind == "date"


def test_null_matches_an_empty_cell_only() -> None:
    table = parse_csv("k,v\na,\nb,1\n")
    assert (
        compare_results(
            table, Table(("k", "v"), (("a", None), ("b", 1))), ["k", "v"], key_columns=["k"]
        )
        is None
    )
    difference = compare_results(
        table, Table(("k", "v"), (("a", 0), ("b", 1))), ["k", "v"], key_columns=["k"]
    )
    assert difference is not None


def test_numeric_keys_sort_alike_on_both_sides() -> None:
    table = parse_csv("year,value\n2025,1\n2026,2\n")
    result = Table(("year", "value"), ((2026, 2), (2025, 1)))
    assert compare_results(table, result, ["year", "value"], key_columns=["year"]) is None


def test_a_difference_names_the_place_and_not_the_values() -> None:
    far = (ROWS[0][0], ROWS[0][1], 0.5)
    difference = same(got(far, ROWS[1], ROWS[2]))
    assert difference is not None
    assert "0.5" not in difference.describe()
    assert "0.5" in difference.describe(verbose=True)
