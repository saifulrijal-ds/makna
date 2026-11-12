-- G36 outstanding_principal over 2026-01-01 to 2026-07-01 (no grain).
-- Rules: a balance over a long range is the balance at the LAST month end in the range
-- (2026-06-30), never a sum of six months.
SELECT SUM(s.outstanding_principal) AS outstanding_principal
FROM core.contract_monthly_snapshots AS s
WHERE s.snapshot_date = (
  SELECT MAX(snapshot_date)
  FROM core.contract_monthly_snapshots
  WHERE snapshot_date >= DATE '2026-01-01' AND snapshot_date < DATE '2026-07-01'
)
