-- G40 outstanding_principal by branch, June 2026.
-- Rules: balance at the last month end in the range (2026-06-30); "branch" means the
-- SERVICING branch (contracts.servicing_branch_id), the branch pinned for portfolio metrics.
SELECT
  b.branch_name AS branch__branch_name,
  SUM(s.outstanding_principal) AS outstanding_principal
FROM core.contract_monthly_snapshots AS s
LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
WHERE s.snapshot_date = DATE '2026-06-30'
GROUP BY b.branch_name
ORDER BY b.branch_name COLLATE "C"
