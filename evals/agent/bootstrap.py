"""The paired bootstrap of the accuracy difference (docs/EVALS.md section 5.4).

The statistic is the mean over questions of (arm M's score - arm R's score), where a question's
score is its mean over its runs. The questions are resampled with replacement, `resamples` times
(10,000 in the report), and the interval is the percentile interval of the resampled means. The
generator is `random.Random(seed)`, so one seed always gives one interval; the report states the
seed.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_RESAMPLES = 10_000
DEFAULT_SEED = 20261111
LEVEL = 0.95


@dataclass(frozen=True)
class Interval:
    mean: float
    """The observed mean difference."""
    low: float
    high: float
    level: float
    resamples: int
    seed: int
    questions: int


def paired_bootstrap(
    differences: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    level: float = LEVEL,
) -> Interval:
    """The percentile interval of the mean of `differences` (one per question).

    With B resamples sorted ascending and a = (1 - level) / 2, the interval is the element at
    index int(B * a) to the element at index B - int(B * a) - 1 (250 and 9749 for B = 10000 and
    95%).
    """
    n = len(differences)
    if n == 0:
        raise ValueError("a bootstrap needs at least one question")
    if resamples < 1 or not 0 < level < 1:
        raise ValueError("resamples must be positive and level between 0 and 1")
    rng = random.Random(seed)
    values = list(differences)
    means = sorted(
        sum(values[int(rng.random() * n)] for _ in range(n)) / n for _ in range(resamples)
    )
    cut = int(resamples * (1 - level) / 2 + 1e-9)
    return Interval(
        sum(values) / n, means[cut], means[resamples - cut - 1], level, resamples, seed, n
    )
