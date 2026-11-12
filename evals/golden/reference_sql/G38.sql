-- G38 active_contract_count per year, no time range (all data).
-- Rules: a stock; each year reports the count at its LAST month end in the data (2024-12-31,
-- 2025-12-31, and 2026-06-30 for the part year), never a sum of months.
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
WITH period_end AS (
  SELECT MAX(snapshot_date) AS snapshot_date
  FROM core.contract_monthly_snapshots
  GROUP BY date_trunc('year', snapshot_date)
)
SELECT
  CAST(date_trunc('year', s.snapshot_date) AS DATE) AS metric_time,
  COUNT(*) AS active_contract_count
FROM core.contract_monthly_snapshots AS s
JOIN period_end AS e ON e.snapshot_date = s.snapshot_date
WHERE COALESCE(s.days_past_due, 0) <= 210
GROUP BY CAST(date_trunc('year', s.snapshot_date) AS DATE)
ORDER BY metric_time
