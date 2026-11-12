-- G13 restructured_ratio with no time range.
-- Rules: balance metric, no range means the latest month end (last snapshot_date). A
-- restructured position is a snapshot row whose CONTRACT has is_restructured = true (rule
-- contract.restructured, a flag on the contracts table reached through
-- contract_monthly_snapshots.contract_id). Ratio = restructured principal / all principal.
WITH agg AS (
  SELECT
    COALESCE(SUM(s.outstanding_principal) FILTER (WHERE c.is_restructured), 0)
      AS restructured_outstanding_principal,
    COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
  FROM core.contract_monthly_snapshots AS s
  LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
  WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
)
SELECT
  restructured_outstanding_principal,
  outstanding_principal,
  CAST(restructured_outstanding_principal AS DOUBLE PRECISION) / NULLIF(outstanding_principal, 0)
    AS restructured_ratio
FROM agg
