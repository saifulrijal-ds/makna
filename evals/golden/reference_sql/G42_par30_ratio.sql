-- G42 candidate par30_ratio by region, no time range.
-- Rules: balance at the latest month end (last snapshot_date); PAR 30 means
-- days_past_due > 30; region from the SERVICING branch through the contract.
WITH agg AS (
  SELECT
    b.region_name AS region_name,
    COALESCE(SUM(s.outstanding_principal) FILTER (WHERE s.days_past_due > 30), 0)
      AS par30_outstanding_principal,
    COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
  FROM core.contract_monthly_snapshots AS s
  LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
  LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
  WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
  GROUP BY b.region_name
)
SELECT
  region_name AS branch__region_name,
  par30_outstanding_principal,
  outstanding_principal,
  CAST(par30_outstanding_principal AS DOUBLE PRECISION) / NULLIF(outstanding_principal, 0)
    AS par30_ratio
FROM agg
ORDER BY region_name COLLATE "C"
