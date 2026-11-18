"""WP-22: the paired bootstrap, with answers known by hand and one seeded regression value."""

from __future__ import annotations

import pytest

from evals.agent.bootstrap import DEFAULT_RESAMPLES, paired_bootstrap


def test_equal_differences_collapse_the_interval_to_that_value() -> None:
    # every resample of five questions that all differ by 0.25 has the mean 0.25
    result = paired_bootstrap([0.25] * 5, seed=3)
    assert (result.mean, result.low, result.high) == (0.25, 0.25, 0.25)
    assert result.resamples == DEFAULT_RESAMPLES == 10_000
    assert (result.level, result.seed, result.questions) == (0.95, 3, 5)


def test_one_question_gives_its_difference() -> None:
    result = paired_bootstrap([-0.5], seed=1)
    assert (result.low, result.mean, result.high) == (-0.5, -0.5, -0.5)


def test_two_questions_give_the_whole_range_by_hand() -> None:
    # Differences 0 and 1: a resample mean is 0, 0.5 or 1 with probability 1/4, 1/2, 1/4. About
    # 2500 of 10000 resamples are 0 (and as many 1), far beyond the 250 cut at each end, so the
    # 95% interval is [0, 1] for every seed.
    for seed in (1, 2, 99):
        result = paired_bootstrap([0.0, 1.0], seed=seed)
        assert (result.mean, result.low, result.high) == (0.5, 0.0, 1.0)


def test_a_seeded_regression_value() -> None:
    differences = [1, 0, 1, 0.5, 1 / 3, -1 / 3, 0, 1]
    result = paired_bootstrap(differences, seed=7)
    assert result.mean == pytest.approx(0.4375)
    assert result.low == pytest.approx(0.10416666666666666)
    assert result.high == pytest.approx(0.7708333333333333)
    assert result == paired_bootstrap(differences, seed=7)
    assert paired_bootstrap(differences, seed=8) != result


def test_the_interval_is_the_percentile_interval_of_the_resampled_means() -> None:
    result = paired_bootstrap([1, 1, 1, 0], seed=5, resamples=1000)
    assert result.low <= result.mean <= result.high
    assert 0 <= result.low and result.high <= 1


def test_bad_arguments_are_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        paired_bootstrap([], seed=1)
    with pytest.raises(ValueError, match="resamples"):
        paired_bootstrap([1.0], seed=1, resamples=0)
    with pytest.raises(ValueError, match="level"):
        paired_bootstrap([1.0], seed=1, level=1.5)
