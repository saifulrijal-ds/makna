-- G16 candidate npf_ratio, no time range.
-- Rules: balance at the latest month end (last snapshot_date); NPF means days_past_due > 90;
-- ratio of non-performing principal to all outstanding principal.
WITH agg AS (
  SELECT
    COALESCE(SUM(s.outstanding_principal) FILTER (WHERE s.days_past_due > 90), 0)
      AS npf_outstanding_principal,
    COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
  FROM core.contract_monthly_snapshots AS s
  WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
)
SELECT
  npf_outstanding_principal,
  outstanding_principal,
  CAST(npf_outstanding_principal AS DOUBLE PRECISION) / NULLIF(outstanding_principal, 0)
    AS npf_ratio
FROM agg
