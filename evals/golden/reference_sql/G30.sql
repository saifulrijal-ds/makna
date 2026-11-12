-- G30 outstanding_principal for the region Jawa Barat, no time range.
-- Rules: balance at the latest month end (last snapshot_date); the filter is the SALES REGION
-- of the SERVICING branch (branches.region_name, not branches.province or the customer's
-- province), reached through contracts.servicing_branch_id.
SELECT COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
FROM core.contract_monthly_snapshots AS s
LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
  AND b.region_name = 'Jawa Barat'
