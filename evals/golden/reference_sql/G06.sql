-- G06 outstanding_principal by days-past-due bucket at the end of June 2026.
-- Rules: balance at the last month end in the range (2026-06-30), not summed over months;
-- the bucket is a column of the snapshot table itself, so no join.
SELECT
  s.dpd_bucket AS contract_snapshot__dpd_bucket,
  SUM(s.outstanding_principal) AS outstanding_principal
FROM core.contract_monthly_snapshots AS s
WHERE s.snapshot_date = DATE '2026-06-30'
GROUP BY s.dpd_bucket
ORDER BY s.dpd_bucket COLLATE "C"
