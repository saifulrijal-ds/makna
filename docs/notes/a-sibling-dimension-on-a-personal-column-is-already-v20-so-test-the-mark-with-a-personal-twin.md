V20 refuses a new dimension that reads a personal column unless it is personal too, so the I-4b mark of the review page is tested with a personal twin

The review page marks a new or changed dimension that reads the same column as a personal dimension (I-4b). A proposal whose new dimension is declared `internal` over `birth_date` never reaches the review queue as a clean proposal: `Check` and `submit` refuse it with V20 ("is not personal but reads personal column ... which would bypass masking"). The mark matters for a dimension that passes V20, for example another personal dimension with the same or a finer masking (a month next to a year), and for an outside change, which is listed whether or not it passes the checks. Test the mark with a personal twin (or an outside commit), and test the refusal separately.

- A relationship cannot be renamed in a proposal either while a metric's `join_paths` names it (V09), so the review tests add a relationship instead of renaming one.
