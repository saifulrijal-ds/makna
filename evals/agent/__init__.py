"""The agent comparison (docs/EVALS.md section 5): what is scored and reported from a runs file.

WP-22 holds the scoring half: the run record (`record`), the reply parser (`reply`), the scorer of
section 5.3 (`scorer`), the personal data scan (`scan`), the paired bootstrap (`bootstrap`) and the
report (`report`). Nothing here calls a model, the network, or a database. The harness that
records the runs is WP-22B.
"""
