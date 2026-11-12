-- G35 par30_ratio by region for sharia products, 2026-04-01 to 2026-07-01 (no grain).
-- Rules: balance metric over a range without grain reports the last month end in the range
-- (2026-06-30), not a sum; PAR 30 means days_past_due > 30; region from the SERVICING branch;
-- the scheme is the contract's product (contracts.product_id -> products.financing_scheme).
WITH agg AS (
  SELECT
    b.region_name AS region_name,
    COALESCE(SUM(s.outstanding_principal) FILTER (WHERE s.days_past_due > 30), 0)
      AS par30_outstanding_principal,
    COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
  FROM core.contract_monthly_snapshots AS s
  LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
  LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
  LEFT JOIN core.products AS p ON p.product_id = c.product_id
  WHERE s.snapshot_date = (
      SELECT MAX(snapshot_date)
      FROM core.contract_monthly_snapshots
      WHERE snapshot_date >= DATE '2026-04-01' AND snapshot_date < DATE '2026-07-01'
    )
    AND p.financing_scheme = 'sharia'
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
