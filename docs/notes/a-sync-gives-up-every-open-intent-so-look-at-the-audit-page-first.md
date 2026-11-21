# `index sync` marks every open intent without a matching commit as failed, so a test that wants a "not completed" audit row must look before the sync

An `audit_event` row with no `commit_sha` and no `failed_at` is an open intent. `makna index sync` (and every lifecycle action, which runs the same step) gives up each open intent that no commit matches: it sets `failed_at`, and the audit page then shows the row as failed, not as "not completed yet". In WP-19B the test for the two marks first inserted an open intent and then called `sync` to log an outside change; only the failed mark was left. Check the page for the open intent before the sync, and the page again after it.

- The same sync logs each outside commit as an `external_change` row with the actor `external:<email>`, and that row only exists after the sync has run, so the review page's "recorded by Makna" line is empty until then.
- A row's `actor_label` of `external:` is not proof of an outside edit: it can be an application commit logged before the S-I2 fix. The review pages decide from the repository state, not from the audit row.
