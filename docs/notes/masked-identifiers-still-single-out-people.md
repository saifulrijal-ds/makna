A partly masked identifier (such as the last four phone digits) used as a grouping key still returns one row per person; mask coarsely and enforce a minimum group size.

Evidence: `docs/reviews/design.md` (R-4) and `docs/reviews/design-probes/output/reident_probe_output.txt`. On the compiler spike's data, NPF amount by masked phone, birth year, and province gave 200 groups for 200 customers, and the last four digits alone identified 198 of 200. An exposure check that looks only for exact raw values reports zero exposures for this.

What the design does now (`docs/ARCHITECTURE.md` section 7.6): no `partial` masking; a personal dimension is masked with `redact`, `truncate_date`, or `bucket`; when a request selects one, groups built from fewer than `min_group_size` records are dropped; only `COUNT` and `COUNT(DISTINCT ...)` may wrap a personal column.
