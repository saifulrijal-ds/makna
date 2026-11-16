When a check needs the columns an expression reads and the expression does not parse, the check must refuse; "no columns" lets a personal column through.

Before WP-15B three functions answered "which columns does this expression read". On text that did not parse, one answered the dimension's own name and two answered an empty set, so V12, V18, V20, R02 and R03 passed because the parse failed. The answer now lives once, in `makna.model.expressions`: it raises `ExpressionError`, and each caller refuses in its own way:

- V12, V18, V20: an issue, when the entity has a personal dimension or the personal list itself has a gap.
- R02, R03: a finding.
- The planner: `BROKEN_REFERENCE`, because the list of personal columns cannot be written and the final check reads that list.
- `effective_level`: `personal`, so the indexer (which may see a file that failed validation) hides the dimension's values.

Do not add a fourth reading of an expression with a `try` that returns an empty result. Call the shared function and decide what your caller refuses.
