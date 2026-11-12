-- G06 outstanding_principal by days-past-due bucket at the end of June 2026.
-- Rules: balance at the last month end in the range (2026-06-30), not summed over months;
-- the bucket is a column of the snapshot table itself, so no join.
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
SELECT
  s.dpd_bucket AS contract_snapshot__dpd_bucket,
  SUM(s.outstanding_principal) AS outstanding_principal
FROM core.contract_monthly_snapshots AS s
WHERE s.snapshot_date = DATE '2026-06-30'
  AND COALESCE(s.days_past_due, 0) <= 210
GROUP BY s.dpd_bucket
ORDER BY s.dpd_bucket COLLATE "C"
