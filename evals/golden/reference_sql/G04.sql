-- G04 npf_ratio by region, June 2026.
-- Rules: balance at the last month end in the range (2026-06-30, never a sum of months);
-- NPF means days_past_due > 90; region comes from the SERVICING branch through the contract
-- (contract_monthly_snapshots -> contracts.servicing_branch_id -> branches).
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
WITH agg AS (
  SELECT
    b.region_name AS region_name,
    COALESCE(SUM(s.outstanding_principal) FILTER (WHERE s.days_past_due > 90), 0)
      AS npf_outstanding_principal,
    COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
  FROM core.contract_monthly_snapshots AS s
  LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
  LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
  WHERE s.snapshot_date = DATE '2026-06-30'
    AND COALESCE(s.days_past_due, 0) <= 210
  GROUP BY b.region_name
)
SELECT
  region_name AS branch__region_name,
  npf_outstanding_principal,
  outstanding_principal,
  CAST(npf_outstanding_principal AS DOUBLE PRECISION) / NULLIF(outstanding_principal, 0)
    AS npf_ratio
FROM agg
ORDER BY region_name COLLATE "C"
