"""The time and grain grammar of ARCHITECTURE 8.4 (WP-20 acceptance 1 and 3), with today =
2026-07-01 (docs/EVALS.md section 1). A range is `[start, end)`: `end` is exclusive."""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

import pytest

import makna.resolve
from makna.resolve.grammar import find_grain, find_times, parse_time
from makna.search.query import tokens

TODAY = datetime.date(2026, 7, 1)
MID_MONTH = datetime.date(2026, 7, 15)


def _range(text: str, today: datetime.date = TODAY) -> tuple[str, str] | None:
    match = parse_time(text, today)
    return None if match is None else (match.start.isoformat(), match.end.isoformat())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # month and year
        ("June 2026", ("2026-06-01", "2026-07-01")),
        ("Juni 2026", ("2026-06-01", "2026-07-01")),
        ("disbursed in december 2025", ("2025-12-01", "2026-01-01")),
        ("Agustus 2025", ("2025-08-01", "2025-09-01")),
        ("Mei 2026", ("2026-05-01", "2026-06-01")),
        # quarter
        ("Q1 2026", ("2026-01-01", "2026-04-01")),
        ("q4 2025", ("2025-10-01", "2026-01-01")),
        ("kuartal 1 2026", ("2026-01-01", "2026-04-01")),
        ("quarter 2 2026", ("2026-04-01", "2026-07-01")),
        ("triwulan I 2026", ("2026-01-01", "2026-04-01")),
        ("triwulan II 2026", ("2026-04-01", "2026-07-01")),
        ("kuartal pertama 2026", ("2026-01-01", "2026-04-01")),
        ("kuartal terakhir 2025", ("2025-10-01", "2026-01-01")),
        ("the second quarter of 2026", ("2026-04-01", "2026-07-01")),
        ("the last quarter of 2025", ("2025-10-01", "2026-01-01")),
        # half year
        ("H1 2026", ("2026-01-01", "2026-07-01")),
        ("h2 2025", ("2025-07-01", "2026-01-01")),
        ("semester 1 2026", ("2026-01-01", "2026-07-01")),
        ("semester 2 2025", ("2025-07-01", "2026-01-01")),
        ("semester II 2025", ("2025-07-01", "2026-01-01")),
        ("paruh pertama 2026", ("2026-01-01", "2026-07-01")),
        ("the first half of 2026", ("2026-01-01", "2026-07-01")),
        ("the second half of 2025", ("2025-07-01", "2026-01-01")),
        # year
        ("2026", ("2026-01-01", "2027-01-01")),
        ("sepanjang 2025", ("2025-01-01", "2026-01-01")),
        ("during 2025", ("2025-01-01", "2026-01-01")),
        # relative to today
        ("last month", ("2026-06-01", "2026-07-01")),
        ("bulan lalu", ("2026-06-01", "2026-07-01")),
        ("last quarter", ("2026-04-01", "2026-07-01")),
        ("kuartal lalu", ("2026-04-01", "2026-07-01")),
        ("triwulan lalu", ("2026-04-01", "2026-07-01")),
        ("this year", ("2026-01-01", "2026-07-01")),
        ("tahun ini", ("2026-01-01", "2026-07-01")),
        ("last year", ("2025-01-01", "2026-01-01")),
        ("tahun lalu", ("2025-01-01", "2026-01-01")),
        ("year to date", ("2026-01-01", "2026-07-01")),
        ("YTD", ("2026-01-01", "2026-07-01")),
        ("tahun berjalan", ("2026-01-01", "2026-07-01")),
        # the end of a period: the last month of it
        ("at the end of June 2026", ("2026-06-01", "2026-07-01")),
        ("di akhir Juni 2026", ("2026-06-01", "2026-07-01")),
        ("at the end of 2025", ("2025-12-01", "2026-01-01")),
        ("end of Q1 2026", ("2026-03-01", "2026-04-01")),
        ("akhir semester 1 2026", ("2026-06-01", "2026-07-01")),
    ],
)
def test_each_phrase_of_section_8_4_gives_its_range(text: str, expected: tuple[str, str]) -> None:
    assert _range(text) == expected


def test_a_question_with_no_time_phrase_has_no_range() -> None:
    assert _range("What is our NPF ratio right now?") is None
    assert _range("PAR 30 by region") is None  # 30 is not a year


def test_this_month_and_this_quarter_run_up_to_today() -> None:
    assert _range("this month", MID_MONTH) == ("2026-07-01", "2026-07-15")
    assert _range("bulan ini", MID_MONTH) == ("2026-07-01", "2026-07-15")
    assert _range("this quarter", MID_MONTH) == ("2026-07-01", "2026-07-15")
    assert _range("kuartal ini", datetime.date(2026, 8, 20)) == ("2026-07-01", "2026-08-20")
    assert _range("this year", MID_MONTH) == ("2026-01-01", "2026-07-15")


def test_on_the_first_day_of_the_period_the_whole_period_is_used_and_the_match_says_so() -> None:
    match = parse_time("this month", TODAY)
    assert match is not None
    assert (match.start.isoformat(), match.end.isoformat()) == ("2026-07-01", "2026-08-01")
    assert match.note is not None and "first day" in match.note
    assert parse_time("this year", datetime.date(2026, 1, 1)) is not None


@pytest.mark.parametrize(
    ("today", "last_month", "last_quarter"),
    [
        (datetime.date(2026, 1, 10), ("2025-12-01", "2026-01-01"), ("2025-10-01", "2026-01-01")),
        (datetime.date(2026, 3, 31), ("2026-02-01", "2026-03-01"), ("2025-10-01", "2026-01-01")),
        (datetime.date(2026, 12, 31), ("2026-11-01", "2026-12-01"), ("2026-07-01", "2026-10-01")),
    ],
)
def test_relative_phrases_move_with_today(
    today: datetime.date, last_month: tuple[str, str], last_quarter: tuple[str, str]
) -> None:
    assert _range("last month", today) == last_month
    assert _range("last quarter", today) == last_quarter


def test_a_period_with_a_year_is_not_read_as_a_relative_phrase() -> None:
    assert _range("last quarter of 2025") == ("2025-10-01", "2026-01-01")
    assert _range("last quarter") == ("2026-04-01", "2026-07-01")


def test_a_month_without_a_year_is_not_a_time_phrase() -> None:
    assert _range("can we see disbursement in june") is None
    assert _range("may I see the NPF ratio") is None


def test_more_than_one_phrase_are_all_found_and_the_first_comes_first() -> None:
    found = find_times(tokens("Q1 2026 and Q2 2026"), TODAY)
    assert [m.start.isoformat() for m in found] == ["2026-01-01", "2026-04-01"]


def test_a_lead_in_belongs_to_the_phrase() -> None:
    words = tokens("total sepanjang 2025")
    (match,) = find_times(words, TODAY)
    assert words[match.first : match.last] == ["sepanjang", "2025"]


@pytest.mark.parametrize(
    ("text", "grain"),
    [
        ("monthly NPF", "month"),
        ("tingkat penagihan bulanan", "month"),
        ("daily disbursement", "day"),
        ("harian", "day"),
        ("weekly", "week"),
        ("mingguan", "week"),
        ("quarterly", "quarter"),
        ("kuartalan", "quarter"),
        ("yearly", "year"),
        ("tahunan", "year"),
        ("sisa pokok per kuartal", "quarter"),
        ("by month", "month"),
        ("per hari", "day"),
        ("per minggu", "week"),
        ("per tahun", "year"),
        ("by quarter", "quarter"),
        ("each year", "year"),
        ("di akhir tiap tahun", "year"),
        ("tiap bulan", "month"),
        ("setiap triwulan", "quarter"),
        ("month by month", "month"),
    ],
)
def test_each_grain_phrase_gives_its_grain(text: str, grain: str) -> None:
    match, missing = find_grain(tokens(text), set(), None)
    assert match is not None and match.grain == grain and not match.trend
    assert not missing


def test_trend_means_the_default_grain_of_the_metric() -> None:
    match, _ = find_grain(tokens("NPF ratio trend"), set(), "quarter")
    assert match is not None and match.grain == "quarter" and match.trend
    match, _ = find_grain(tokens("tren NPF"), set(), "month")
    assert match is not None and match.grain == "month"
    match, missing = find_grain(tokens("NPF ratio trend"), set(), None)
    assert match is None and missing


def test_a_period_word_alone_is_not_a_grain() -> None:
    assert find_grain(tokens("in the last 3 months"), set(), None) == (None, False)
    assert find_grain(tokens("bulan ini"), set(), None) == (None, False)


def test_words_that_another_phrase_took_are_not_a_grain() -> None:
    words = tokens("per bulan")
    assert find_grain(words, {1}, None) == (None, False)


# --- criterion 3: the grammar never reads the clock ------------------------------------------


def test_a_frozen_clock_at_another_date_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    texts = ["last month", "this year", "year to date", "kuartal lalu", "June 2026", "tahun ini"]
    before = [_range(t) for t in texts]

    class Frozen(datetime.date):
        @classmethod
        def today(cls) -> Frozen:
            return cls(1999, 12, 31)

    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> FrozenDateTime:
            return cls(1999, 12, 31, 23, 59)

        @classmethod
        def today(cls) -> FrozenDateTime:
            return cls(1999, 12, 31, 23, 59)

        @classmethod
        def utcnow(cls) -> FrozenDateTime:  # noqa: D102 - deprecated, frozen all the same
            return cls(1999, 12, 31, 23, 59)

    monkeypatch.setattr(datetime, "date", Frozen)
    monkeypatch.setattr(datetime, "datetime", FrozenDateTime)
    monkeypatch.setattr(time, "time", lambda: 946_684_799.0)
    assert datetime.date.today() == datetime.date(1999, 12, 31)  # the freeze works
    assert [_range(t) for t in texts] == before
    assert _range("last month", datetime.date(2026, 7, 1)) == ("2026-06-01", "2026-07-01")


_CLOCK_CALLS = {
    "today", "now", "utcnow", "time", "time_ns", "monotonic", "perf_counter", "localtime",
    "gmtime", "strftime", "fromtimestamp",
}  # fmt: skip


def test_the_resolver_source_never_asks_for_the_time() -> None:
    root = Path(makna.resolve.__file__).parent
    files = sorted(root.glob("*.py"))
    assert len(files) >= 3
    problems: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                if any(n.split(".")[0] == "time" for n in names):
                    problems.append(f"{path.name}:{node.lineno} imports time")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _CLOCK_CALLS
            ):
                problems.append(f"{path.name}:{node.lineno} calls .{node.func.attr}()")
    assert problems == []
