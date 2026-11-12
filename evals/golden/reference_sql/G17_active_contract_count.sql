-- G17 candidate active_contract_count, no time range.
-- Rules: a stock counted at the latest month end: one snapshot row per contract that still
-- owes principal at that date (the last snapshot_date).
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
SELECT COUNT(*) AS active_contract_count
FROM core.contract_monthly_snapshots AS s
WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
  AND COALESCE(s.days_past_due, 0) <= 210
