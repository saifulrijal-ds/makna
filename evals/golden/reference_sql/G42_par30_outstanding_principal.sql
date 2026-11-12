-- G42 candidate par30_outstanding_principal by region, no time range.
-- Rules: balance at the latest month end (last snapshot_date); PAR 30 means
-- days_past_due > 30; region from the SERVICING branch through the contract.
SELECT
  b.region_name AS branch__region_name,
  COALESCE(SUM(s.outstanding_principal), 0) AS par30_outstanding_principal
FROM core.contract_monthly_snapshots AS s
LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
  AND s.days_past_due > 30
GROUP BY b.region_name
ORDER BY b.region_name COLLATE "C"
