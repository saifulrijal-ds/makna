-- G23 promise_to_pay_rate, June 2026.
-- Rules: official date is the action date (collection_actions.tgl_aksi); rate = activities
-- with outcome (hasil) 'promise_to_pay' divided by all collection activities, both counted
-- on the same date range; computed after counting.
WITH agg AS (
  SELECT
    COUNT(*) FILTER (WHERE ca.hasil = 'promise_to_pay') AS promise_to_pay_count,
    COUNT(*) AS collection_action_count
  FROM core.collection_actions AS ca
  WHERE ca.tgl_aksi >= DATE '2026-06-01'
    AND ca.tgl_aksi < DATE '2026-07-01'
)
SELECT
  promise_to_pay_count,
  collection_action_count,
  CAST(promise_to_pay_count AS DOUBLE PRECISION) / NULLIF(collection_action_count, 0)
    AS promise_to_pay_rate
FROM agg
