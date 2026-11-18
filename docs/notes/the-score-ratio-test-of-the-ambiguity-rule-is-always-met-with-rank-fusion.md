With rank fusion the "second score is at least 0.85 of the first" half of the ambiguity rule is almost always true, so the coverage half alone decides what is ambiguous.

Evidence (WP-20, 2026-11-18, MiniLM, committed weights): the fused scores of the first four metric hits of a golden question differ by 1 to 3 percent (`0.01844`, `0.01815`, `0.01780`, ...), because a rank 1 and a rank 2 of one list differ by (60+1)/(60+2). Rank 1 against rank 10 of a list is still 0.87. The rule of `docs/ARCHITECTURE.md` 8.4 (score ratio 0.85 and coverage gap 0.15) therefore reduces to "the coverage of the two candidates differs by less than 0.15".

What follows:

- A question whose words are in the names of two metrics equally is ambiguous, whichever name is longer. "Outstanding principal" is in `outstanding_principal` and in `restructured_outstanding_principal`; "disburse" is in `disbursement_amount` and `disbursed_contract_count`. On the golden set this makes 12 of 36 `answer` questions `ambiguous` (`docs/EVALS.md` section 6).
- A signal for the tighter name (the share of the metric's own name that the question uses) separates those, but it also separates "Berapa NPF kita?" (G16), which must stay ambiguous between the ratio and the amount. One number cannot do both; do not add a tie-break to fix the count of one question. Changing the rule is the owner's decision.
- If the score test is ever to mean something, it needs a score that is not a rank fusion (for example the keyword score or the cosine of the two hits), not a different threshold.

The two constants and the one function are in `makna.search.confidence` (`AMBIGUITY_SCORE_RATIO`, `AMBIGUITY_COVERAGE_GAP`, `is_ambiguous`), used by the resolver and the search service.
