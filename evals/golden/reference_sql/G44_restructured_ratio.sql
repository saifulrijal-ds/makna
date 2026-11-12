-- G44 candidate restructured_ratio, June 2026.
-- Rules: balance at the last month end in the range (2026-06-30); restructured means the
-- CONTRACT has is_restructured = true; ratio of restructured principal to all principal.
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
WITH agg AS (
  SELECT
    COALESCE(SUM(s.outstanding_principal) FILTER (WHERE c.is_restructured), 0)
      AS restructured_outstanding_principal,
    COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
  FROM core.contract_monthly_snapshots AS s
  LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
  WHERE s.snapshot_date = DATE '2026-06-30'
    AND COALESCE(s.days_past_due, 0) <= 210
)
SELECT
  restructured_outstanding_principal,
  outstanding_principal,
  CAST(restructured_outstanding_principal AS DOUBLE PRECISION) / NULLIF(outstanding_principal, 0)
    AS restructured_ratio
FROM agg
