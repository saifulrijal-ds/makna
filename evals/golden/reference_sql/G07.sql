-- G07 outstanding_principal per quarter, 2025-01-01 to 2026-01-01.
-- Rules: balance metric; each quarter reports the balance at its LAST month end inside the
-- range (2025-03-31, 06-30, 09-30, 12-31), never a sum of the three months.
WITH period_end AS (
  SELECT MAX(snapshot_date) AS snapshot_date
  FROM core.contract_monthly_snapshots
  WHERE snapshot_date >= DATE '2025-01-01'
    AND snapshot_date < DATE '2026-01-01'
  GROUP BY date_trunc('quarter', snapshot_date)
)
SELECT
  CAST(date_trunc('quarter', s.snapshot_date) AS DATE) AS metric_time,
  SUM(s.outstanding_principal) AS outstanding_principal
FROM core.contract_monthly_snapshots AS s
JOIN period_end AS e ON e.snapshot_date = s.snapshot_date
GROUP BY CAST(date_trunc('quarter', s.snapshot_date) AS DATE)
ORDER BY metric_time
