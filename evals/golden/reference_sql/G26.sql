-- G26 npf_outstanding_principal by region, at the end of May 2026.
-- Rules: balance at the last month end in the range (2026-05-31); non-performing means
-- days_past_due > 90; region from the SERVICING branch through the contract.
SELECT
  b.region_name AS branch__region_name,
  COALESCE(SUM(s.outstanding_principal), 0) AS npf_outstanding_principal
FROM core.contract_monthly_snapshots AS s
LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
WHERE s.snapshot_date = DATE '2026-05-31'
  AND s.days_past_due > 90
GROUP BY b.region_name
ORDER BY b.region_name COLLATE "C"
