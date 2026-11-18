"""The time and grain grammar of ARCHITECTURE section 8.4, in English and Indonesian.

Pure functions over a list of lower-case words (`makna.search.query.tokens`) and a date. Nothing
here reads the clock: `today` is always a parameter, so the same question and the same date give
the same range wherever and whenever it runs.

**Ranges.** A range is `[start, end)`: `start` is the first day and `end` the first day after the
period, because the compiler's `time_start` is inclusive and `time_end` is exclusive (section 7.1).
"June 2026" is 2026-06-01 up to 2026-07-01.

**Periods that are not over.** "This year" and "year to date" run from the first day of the year up
to `today` (exclusive), as `docs/EVALS.md` section 1 states: the day that is not over has no
complete data. "This month" and "this quarter" read the same way. On the first day of the period
that range is empty, so the whole period is used and the match says so in its `note`.

**End of a period.** "End of June 2026" is June 2026 (a balance metric reports the last month end
of a period anyway); "end of 2025", "end of Q1 2026", and "akhir semester 1 2026" are the last month
of the period.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass

from makna.search.query import tokens
from makna.search.terms import (
    EACH_WORDS,
    END_WORDS,
    FINAL_WORDS,
    GRAIN_ADJECTIVES,
    GRAIN_PHRASES,
    GRAIN_WORDS,
    MONTHS,
    ORDINALS,
    TIME_LEAD_INS,
    TIME_PHRASES,
    TREND_WORDS,
)

MIN_YEAR, MAX_YEAR = 1900, 2100
QUARTER_WORDS = frozenset({"quarter", "kuartal", "triwulan"})
HALF_WORDS = frozenset({"half", "semester", "paruh"})
_QUARTER_TOKENS = {"q1": 1, "q2": 2, "q3": 3, "q4": 4}
_HALF_TOKENS = {"h1": 1, "h2": 2}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
_GRAIN_NOUNS = {word: grain for word, grain in GRAIN_WORDS.items() if word not in GRAIN_ADJECTIVES}
"""Words that name a period length ("month", "bulan"); the adjectives are `GRAIN_ADJECTIVES`."""


@dataclass(frozen=True)
class TimeMatch:
    """A time phrase found in a question, read against `today`."""

    start: datetime.date
    end: datetime.date
    """The first day after the period: exclusive, as the compiler's `time_end`."""
    kind: str
    """`month`, `quarter`, `half`, `year`, or the symbolic phrase (`last_month`, ...)."""
    first: int
    last: int
    """The words the phrase uses are `words[first:last]`."""
    note: str | None = None


@dataclass(frozen=True)
class GrainMatch:
    grain: str
    first: int
    last: int
    trend: bool = False
    """True when the grain is the metric's default because the question asks for a trend."""


# --- calendar helpers -----------------------------------------------------------------------


def _month(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    start = datetime.date(year, month, 1)
    return start, _add_months(start, 1)


def _add_months(day: datetime.date, months: int) -> datetime.date:
    index = day.year * 12 + (day.month - 1) + months
    return datetime.date(index // 12, index % 12 + 1, 1)


def _quarter(year: int, quarter: int) -> tuple[datetime.date, datetime.date]:
    start = datetime.date(year, 3 * quarter - 2, 1)
    return start, _add_months(start, 3)


def _half(year: int, half: int) -> tuple[datetime.date, datetime.date]:
    start = datetime.date(year, 6 * half - 5, 1)
    return start, _add_months(start, 6)


def _year(year: int) -> tuple[datetime.date, datetime.date]:
    return datetime.date(year, 1, 1), datetime.date(year + 1, 1, 1)


def _year_number(word: str) -> int | None:
    if len(word) == 4 and word.isdigit() and MIN_YEAR <= int(word) <= MAX_YEAR:
        return int(word)
    return None


def _number(word: str, *, quarters: bool) -> int | None:
    """A quarter or half number written as a digit, a Roman numeral, or an ordinal word; the last
    one of the year for "terakhir" and "final"."""
    top = 4 if quarters else 2
    if word.isdigit() and 1 <= int(word) <= top:
        return int(word)
    if word in _ROMAN and _ROMAN[word] <= top:
        return _ROMAN[word]
    if word in ORDINALS and ORDINALS[word] <= top:
        return ORDINALS[word]
    if word in FINAL_WORDS:
        return top
    return None


def _spelled_number(word: str, *, quarters: bool) -> int | None:
    """The number before a period word ("first half", "last quarter"): an ordinal or "last"."""
    if word == "last":
        return 4 if quarters else 2
    if word in ORDINALS or word in FINAL_WORDS:
        return _number(word, quarters=quarters)
    return None


# --- the time grammar ------------------------------------------------------------------------


def _span(kind: str, year: int, number: int) -> tuple[datetime.date, datetime.date]:
    return _quarter(year, number) if kind == "quarter" else _half(year, number)


def _period_at(
    words: Sequence[str], i: int
) -> tuple[str, datetime.date, datetime.date, int] | None:
    """A named period that starts at word `i`: `(kind, start, end, words used)`."""
    here = words[i]
    after = words[i + 1] if i + 1 < len(words) else ""
    year_after = _year_number(after)
    if here in MONTHS and year_after is not None:
        return ("month", *_month(year_after, MONTHS[here]), 2)
    if here in _QUARTER_TOKENS and year_after is not None:
        return ("quarter", *_quarter(year_after, _QUARTER_TOKENS[here]), 2)
    if here in _HALF_TOKENS and year_after is not None:
        return ("half", *_half(year_after, _HALF_TOKENS[here]), 2)
    for kind, names in (("quarter", QUARTER_WORDS), ("half", HALF_WORDS)):
        quarters = kind == "quarter"
        if here in names and i + 2 < len(words):
            # "quarter 1 2026", "kuartal pertama 2026", "semester II 2026", "paruh pertama 2026"
            number, year = _number(after, quarters=quarters), _year_number(words[i + 2])
            if number is not None and year is not None:
                return (kind, *_span(kind, year, number), 3)
        number = _spelled_number(here, quarters=quarters)
        if number is not None and after in names:
            # "first half of 2026", "the last quarter of 2025", "second quarter 2026"
            j = i + 2 + (1 if i + 2 < len(words) and words[i + 2] == "of" else 0)
            year = _year_number(words[j]) if j < len(words) else None
            if year is not None:
                return (kind, *_span(kind, year, number), j + 1 - i)
    if year_after is None and (year := _year_number(here)) is not None:
        return ("year", *_year(year), 1)
    return None


def _last_month_of(
    kind: str, start: datetime.date, end: datetime.date
) -> tuple[datetime.date, datetime.date]:
    """The last month of a period (a month is its own last month)."""
    if kind == "month":
        return start, end
    return _add_months(end, -1), end


def _relative(symbol: str, today: datetime.date) -> tuple[datetime.date, datetime.date, str | None]:
    """A symbolic phrase of `TIME_PHRASES` against `today`: `(start, end, note)`."""
    this_month = today.replace(day=1)
    quarter_start = datetime.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    if symbol == "last_month":
        return _add_months(this_month, -1), this_month, None
    if symbol == "last_quarter":
        return _add_months(quarter_start, -3), quarter_start, None
    if symbol == "last_year":
        return datetime.date(today.year - 1, 1, 1), datetime.date(today.year, 1, 1), None
    if symbol in ("this_year", "year_to_date"):
        start, whole = datetime.date(today.year, 1, 1), _year(today.year)[1]
    elif symbol == "this_month":
        start, whole = this_month, _add_months(this_month, 1)
    else:  # this_quarter
        start, whole = quarter_start, _add_months(quarter_start, 3)
    if today > start:
        return start, today, None
    return start, whole, f"today ({today.isoformat()}) is the first day of the period, so all of it"


def _match_at(words: Sequence[str], i: int, today: datetime.date) -> TimeMatch | None:
    """The longest time phrase that starts at word `i`."""
    # "end of June 2026", "akhir 2025": the last month of the period that follows.
    end_marker = 0
    if words[i] in END_WORDS:
        end_marker = 2 if i + 1 < len(words) and words[i + 1] == "of" else 1
    # A period with a year first: "last quarter of 2025" must not read as the relative phrase.
    found = _period_at(words, i + end_marker)
    if found is not None:
        kind, start, end, used = found
        if end_marker:
            start, end = _last_month_of(kind, start, end)
        return TimeMatch(start, end, kind, i, i + end_marker + used)
    if end_marker:
        return None
    for count in (3, 2, 1):
        phrase = " ".join(words[i : i + count])
        symbol = TIME_PHRASES.get(phrase)
        if symbol is not None and len(phrase.split()) == count:
            start, end, note = _relative(symbol, today)
            return TimeMatch(start, end, symbol, i, i + count, note)
    return None


def find_times(words: Sequence[str], today: datetime.date) -> list[TimeMatch]:
    """Every time phrase of `words`, left to right, none overlapping the one before.

    A lead-in word right before a phrase ("during 2025", "sepanjang 2025") is part of it.
    """
    found: list[TimeMatch] = []
    i = 0
    while i < len(words):
        match = _match_at(words, i, today)
        if match is None:
            i += 1
            continue
        first = match.first
        while first > 0 and (
            words[first - 1] in TIME_LEAD_INS
            # "over the first half of 2026": one "the" may stand between the lead-in and the phrase
            or (words[first - 1] == "the" and first > 1 and words[first - 2] in TIME_LEAD_INS)
        ):
            first -= 1
        found.append(TimeMatch(match.start, match.end, match.kind, first, match.last, match.note))
        i = match.last
    return found


def parse_time(text: str, today: datetime.date) -> TimeMatch | None:
    """The first time phrase of a question text, or None."""
    found = find_times(tokens(text), today)
    return found[0] if found else None


# --- the grain grammar -----------------------------------------------------------------------


def find_grain(
    words: Sequence[str], taken: set[int], default_grain: str | None
) -> tuple[GrainMatch | None, bool]:
    """The grain a question asks for, from words that no other phrase uses (`taken`).

    "monthly", "per bulan", "by quarter", "each year", "tiap bulan", "month by month" ask for a
    grain; "trend" or "tren" asks for `default_grain`. The first such phrase wins. Returns the
    match and whether the question asks for a trend with no default grain to give it.
    """
    trend_without_default = False
    for i, word in enumerate(words):
        if i in taken:
            continue
        pair = " ".join(words[i : i + 2])
        if i + 1 < len(words) and i + 1 not in taken and pair in GRAIN_PHRASES:
            return GrainMatch(GRAIN_PHRASES[pair], i, i + 2), False
        if word in GRAIN_ADJECTIVES:
            return GrainMatch(GRAIN_ADJECTIVES[word], i, i + 1), False
        following = words[i + 1] if i + 1 < len(words) and i + 1 not in taken else None
        if word in EACH_WORDS and following in _GRAIN_NOUNS:
            # "at the end of each year", "di akhir tiap tahun": the end belongs to the phrase
            start = i
            if i >= 2 and words[i - 2] in END_WORDS and words[i - 1] == "of":
                start = i - 2
            elif i >= 1 and words[i - 1] in END_WORDS:
                start = i - 1
            if start not in taken:
                return GrainMatch(_GRAIN_NOUNS[following], start, i + 2), False
            return GrainMatch(_GRAIN_NOUNS[following], i, i + 2), False
        if (
            word in _GRAIN_NOUNS
            and i + 2 < len(words)
            and words[i + 1] == "by"
            and words[i + 2] == word
            and {i + 1, i + 2}.isdisjoint(taken)
        ):
            return GrainMatch(_GRAIN_NOUNS[word], i, i + 3), False
        if word in TREND_WORDS:
            if default_grain is None:
                trend_without_default = True
                continue
            return GrainMatch(default_grain, i, i + 1, trend=True), False
    return None, trend_without_default
