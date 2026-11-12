-- G25 par30_outstanding_principal by region, at the end of June 2026.
-- Rules: balance at the last month end in the range (2026-06-30); PAR 30 means
-- days_past_due > 30 (this includes the non-performing positions); region from the
-- SERVICING branch through the contract.
SELECT
  b.region_name AS branch__region_name,
  COALESCE(SUM(s.outstanding_principal), 0) AS par30_outstanding_principal
FROM core.contract_monthly_snapshots AS s
LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
WHERE s.snapshot_date = DATE '2026-06-30'
  AND s.days_past_due > 30
GROUP BY b.region_name
ORDER BY b.region_name COLLATE "C"
