-- G44 candidate restructured_ratio, June 2026.
-- Rules: balance at the last month end in the range (2026-06-30); restructured means the
-- CONTRACT has is_restructured = true; ratio of restructured principal to all principal.
WITH agg AS (
  SELECT
    COALESCE(SUM(s.outstanding_principal) FILTER (WHERE c.is_restructured), 0)
      AS restructured_outstanding_principal,
    COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
  FROM core.contract_monthly_snapshots AS s
  LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
  WHERE s.snapshot_date = DATE '2026-06-30'
)
SELECT
  restructured_outstanding_principal,
  outstanding_principal,
  CAST(restructured_outstanding_principal AS DOUBLE PRECISION) / NULLIF(outstanding_principal, 0)
    AS restructured_ratio
FROM agg
