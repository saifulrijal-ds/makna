"""The time and grain vocabulary of ARCHITECTURE section 8.4, in English and Indonesian.

One module, two users: the confidence band of the search (section 8.3) leaves these words out
of the coverage calculation, because they describe a period or a cut and never name a metric;
the resolver (WP-20) reads the same tables to find the time range and the grain of a question.
Words are lower case, as `makna.search.query.words` produces them.
"""

from __future__ import annotations

from types import MappingProxyType

MONTHS: MappingProxyType[str, int] = MappingProxyType(
    {
        "january": 1, "jan": 1, "januari": 1,
        "february": 2, "feb": 2, "februari": 2,
        "march": 3, "mar": 3, "maret": 3,
        "april": 4, "apr": 4,
        "may": 5, "mei": 5,
        "june": 6, "jun": 6, "juni": 6,
        "july": 7, "jul": 7, "juli": 7,
        "august": 8, "aug": 8, "agustus": 8, "agu": 8, "agt": 8,
        "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10, "oktober": 10, "okt": 10,
        "november": 11, "nov": 11, "nop": 11,
        "december": 12, "dec": 12, "desember": 12, "des": 12,
    }
)  # fmt: skip
"""Month names and abbreviations to the month number."""

GRAIN_WORDS: MappingProxyType[str, str] = MappingProxyType(
    {
        "daily": "day", "harian": "day", "day": "day", "days": "day", "hari": "day",
        "weekly": "week", "mingguan": "week", "week": "week", "weeks": "week",
        "minggu": "week",
        "monthly": "month", "bulanan": "month", "month": "month", "months": "month",
        "bulan": "month",
        "quarterly": "quarter", "kuartalan": "quarter", "quarter": "quarter",
        "quarters": "quarter", "kuartal": "quarter", "triwulan": "quarter",
        "triwulanan": "quarter",
        "yearly": "year", "annual": "year", "annually": "year", "tahunan": "year",
        "year": "year", "years": "year", "tahun": "year",
    }
)  # fmt: skip
"""Words that name a grain or a period length, to the grain."""

HALF_YEAR_WORDS = frozenset({"semester", "half", "h1", "h2", "paruh"})
"""Words of a half-year phrase ("H1 2026", "semester 1 2026")."""

QUARTER_NUMBER_WORDS = frozenset({"q1", "q2", "q3", "q4"})
"""A quarter written as one token ("Q1 2026")."""

TREND_WORDS = frozenset({"trend", "tren"})
"""A trend question means the metric's default grain."""

RELATIVE_WORDS = frozenset(
    {
        "last", "past", "previous", "this", "current", "next", "ago",
        "lalu", "kemarin", "ini", "sekarang", "berjalan", "depan", "terakhir",
        "ytd", "mtd", "qtd", "date", "to",
        "sampai", "hingga", "dari", "from", "until", "since", "sejak", "during", "selama",
        "end", "akhir", "start", "awal", "per",
    }
)  # fmt: skip
"""Words that make a period relative to today or bound it."""

ROMAN_NUMERALS = frozenset({"i", "ii", "iii", "iv"})
"""Quarter numbers written in Roman numerals ("triwulan II 2026")."""

TIME_PHRASES: MappingProxyType[str, str] = MappingProxyType(
    {
        "last month": "last_month", "bulan lalu": "last_month",
        "this month": "this_month", "bulan ini": "this_month",
        "last quarter": "last_quarter", "kuartal lalu": "last_quarter",
        "triwulan lalu": "last_quarter",
        "this quarter": "this_quarter", "kuartal ini": "this_quarter",
        "triwulan ini": "this_quarter",
        "last year": "last_year", "tahun lalu": "last_year",
        "this year": "this_year", "tahun ini": "this_year",
        "year to date": "year_to_date", "ytd": "year_to_date",
        "tahun berjalan": "year_to_date",
    }
)  # fmt: skip
"""Relative time phrases to a symbolic range the resolver evaluates against `today`."""

GRAIN_PHRASES: MappingProxyType[str, str] = MappingProxyType(
    {
        "per hari": "day", "per day": "day", "by day": "day",
        "per minggu": "week", "per week": "week", "by week": "week",
        "per bulan": "month", "per month": "month", "by month": "month",
        "per kuartal": "quarter", "per triwulan": "quarter", "per quarter": "quarter",
        "by quarter": "quarter",
        "per tahun": "year", "per year": "year", "by year": "year",
    }
)  # fmt: skip
"""Phrases that ask for a grain."""

QUALIFIER_WORDS: frozenset[str] = frozenset(
    set(MONTHS)
    | set(GRAIN_WORDS)
    | HALF_YEAR_WORDS
    | QUARTER_NUMBER_WORDS
    | TREND_WORDS
    | RELATIVE_WORDS
    | ROMAN_NUMERALS
)
"""Every word of the vocabulary: a query word in this set is a qualifier, not a content word."""


def is_time_word(word: str) -> bool:
    """True for a word of the vocabulary above, or a number (a year, a quarter, a day)."""
    return word in QUALIFIER_WORDS or word.isdigit()
