`GovernanceError` for a catalog-tagged column is reachable only when no dimension declares the column's level, because R03 comes first when the sync sees the definitions.

Seen while building WP-12 (2026-11-13). Tagging `contracts.disbursement_date` personal in the catalog and syncing with the definitions opens R03 on `entity:contract` (the dimension `disbursement_date` is declared internal), and the planner then refuses every request through that entity with `BROKEN_REFERENCE` before the final check runs. The final check is what stops these cases:

- a column read only by a join key (`contracts.customer_id`), a rule, a measure filter, or a metric filter, which no dimension reads;
- a catalog synced without definitions (`makna catalog sync` with no definitions directory skips the reference check).

Tests for R05 therefore sync with `entities=None` (`check_references=False` in `tests/integration/service_support.py`), as the WP-10B experiments did by building the model without issues.
