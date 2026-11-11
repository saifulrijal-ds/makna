"""The confidence band on a small hand-made vocabulary (ARCHITECTURE 8.3)."""

from __future__ import annotations

import pytest

from makna.model.search import ConfidenceSettings
from makna.search.confidence import (
    LOW_GUIDANCE,
    assess,
    build_vocabulary,
    matched_terms,
    split_words,
)

DOCUMENTS = [
    ("metric", "npf ratio ; NPF Ratio ; rasio NPF ; kredit macet", ""),
    ("metric", "disbursement amount ; pencairan ; jumlah pencairan", ""),
    ("metric", "collection rate ; tingkat penagihan", ""),
    ("glossary_term", "tenor ; jangka waktu", ""),
    ("dimension", "region name ; wilayah ; sales region", "jawa sumatra"),
    ("dimension", "product category ; kategori produk", "mobil motor"),
    ("entity", "branch ; cabang", ""),
]
VOCABULARY = build_vocabulary(DOCUMENTS)
SETTINGS = ConfidenceSettings()


def test_dimension_words_that_name_no_metric_are_qualifiers() -> None:
    content, qualifiers = split_words(["pencairan", "wilayah", "region", "tahun"], VOCABULARY)
    assert content == ["pencairan"]
    assert qualifiers == ["wilayah", "region", "tahun"]


def test_declared_values_are_qualifier_words_too() -> None:
    content, qualifiers = split_words(["npf", "sumatra"], VOCABULARY)
    assert (content, qualifiers) == (["npf"], ["sumatra"])


def test_a_word_that_names_a_metric_and_a_dimension_stays_content() -> None:
    vocabulary = build_vocabulary(
        [("metric", "region count", ""), ("dimension", "region name", "")]
    )
    assert split_words(["region"], vocabulary) == (["region"], [])


def test_full_coverage_is_high() -> None:
    result = assess(
        ["pencairan", "wilayah"], ["disbursement amount ; pencairan"], [0.7], VOCABULARY, SETTINGS
    )
    assert result.band == "high" and result.coverage == pytest.approx(1.0)
    assert result.guidance is None


def test_no_coverage_is_low_with_guidance() -> None:
    result = assess(["website", "conversion"], ["npf ratio"], [0.2], VOCABULARY, SETTINGS)
    assert result.band == "low" and result.coverage == 0.0
    assert result.guidance == LOW_GUIDANCE


def test_a_query_with_only_qualifier_words_is_low() -> None:
    result = assess(["wilayah", "tahun"], ["region name ; wilayah"], [0.9], VOCABULARY, SETTINGS)
    assert result.band == "low" and result.content_words == ()


def test_partial_coverage_is_medium() -> None:
    # "kredit" is covered by the top hit, "website" (rarer, so heavier) is not
    result = assess(
        ["kredit", "website"], ["npf ratio ; kredit macet"], [0.3], VOCABULARY, SETTINGS
    )
    assert 0.3 <= result.coverage < 0.6
    assert result.band == "medium"


def test_only_the_top_three_hits_count() -> None:
    names = ["branch ; cabang", "tenor", "collection rate", "npf ratio ; kredit macet"]
    assert assess(["kredit"], names, [0.5] * 4, VOCABULARY, SETTINGS).band == "low"
    assert assess(["kredit"], names[::-1], [0.5] * 4, VOCABULARY, SETTINGS).band == "high"


def test_a_high_cosine_can_keep_a_weak_coverage_out_of_low_once_calibrated() -> None:
    calibrated = ConfidenceSettings(low_cosine=0.5)
    args = (["website"], ["npf ratio"])
    assert assess(*args, [0.4], VOCABULARY, calibrated).band == "low"
    assert assess(*args, [0.8], VOCABULARY, calibrated).band == "medium"
    assert assess(*args, [0.8], VOCABULARY, SETTINGS).band == "low"  # uncalibrated: coverage only


def test_idf_makes_rare_words_weigh_more() -> None:
    vocabulary = build_vocabulary(
        [("metric", f"ratio item{i}", "") for i in range(5)] + [("metric", "macet", "")]
    )
    assert vocabulary.idf("macet") > vocabulary.idf("ratio")
    assert vocabulary.idf("unseen") > vocabulary.idf("macet")


def test_matched_terms_lists_the_query_words_found_in_a_hit() -> None:
    assert matched_terms(["npf", "region", "disburse"], "npf ratio ; disbursement") == [
        "npf",
        "disburse",
    ]
