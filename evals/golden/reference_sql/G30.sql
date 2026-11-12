-- G30 outstanding_principal for the region Jawa Barat, no time range.
-- Rules: balance at the latest month end (last snapshot_date); the filter is the SALES REGION
-- of the SERVICING branch (branches.region_name, not branches.province or the customer's
-- province), reached through contracts.servicing_branch_id.
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
SELECT COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
FROM core.contract_monthly_snapshots AS s
LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
  AND COALESCE(s.days_past_due, 0) <= 210
  AND b.region_name = 'Jawa Barat'
