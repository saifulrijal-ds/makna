"""Query processing and the time and grain vocabulary (ARCHITECTURE 4.1 step 2 and 8.4)."""

from __future__ import annotations

import pytest

from makna.search import terms
from makna.search.query import STOP_WORDS, process, word_matches, words


def test_words_are_lower_case_and_split_on_punctuation() -> None:
    assert words("NPF-ratio, per Wilayah!") == ["npf", "ratio", "per", "wilayah"]


def test_words_keep_digits_and_drop_repeats() -> None:
    assert words("Q1 2026 q1") == ["q1", "2026"]


def test_stop_words_of_both_languages_are_dropped() -> None:
    assert process("What is the total disbursement of the year?").words == (
        "disbursement",
        "year",
    )
    assert process("Berapa total pencairan untuk tiap wilayah?").words == ("pencairan", "wilayah")


def test_rate_and_ratio_are_not_stop_words() -> None:
    assert not {"rate", "ratio", "npf", "pencairan"} & STOP_WORDS


def test_the_tsquery_ors_words_and_prefix_matches_from_four_letters() -> None:
    assert process("npf disbursement").tsquery == "'npf' | 'disbursement':*"
    assert process("the of a").tsquery is None


def test_a_query_with_quotes_cannot_break_the_tsquery() -> None:
    assert process('it\'s a "test"; DROP TABLE x --').tsquery == ("'test':* | 'drop':* | 'table':*")


@pytest.mark.parametrize(
    ("word", "token", "expected"),
    [
        ("npf", "npf", True),
        ("npf", "npfx", False),  # prefix match needs four letters
        ("disburse", "disbursement", True),
        ("disb", "disbursement", True),
        ("cost", "disbursement", False),
    ],
)
def test_word_matches(word: str, token: str, expected: bool) -> None:
    assert word_matches(word, token) is expected


@pytest.mark.parametrize(
    "word",
    ["tahun", "year", "bulan", "kuartal", "triwulan", "juni", "june", "ytd", "lalu", "last",
     "semester", "q1", "2026", "harian", "monthly", "tren"],
)  # fmt: skip
def test_time_and_grain_words_are_qualifiers(word: str) -> None:
    assert terms.is_time_word(word)


@pytest.mark.parametrize("word", ["npf", "pencairan", "wilayah", "region", "ratio", "disbursement"])
def test_metric_and_dimension_words_are_not_time_words(word: str) -> None:
    assert not terms.is_time_word(word)


def test_the_vocabulary_maps_to_the_values_the_resolver_needs() -> None:
    assert terms.MONTHS["juni"] == terms.MONTHS["june"] == 6
    assert terms.MONTHS["agustus"] == 8 and terms.MONTHS["mei"] == 5
    assert terms.GRAIN_WORDS["bulanan"] == "month"
    assert terms.GRAIN_WORDS["triwulan"] == "quarter"
    assert terms.GRAIN_PHRASES["per kuartal"] == "quarter"
    assert terms.TIME_PHRASES["tahun ini"] == "this_year"
    assert terms.TIME_PHRASES["bulan lalu"] == terms.TIME_PHRASES["last month"] == "last_month"
    assert terms.TIME_PHRASES["ytd"] == "year_to_date"
