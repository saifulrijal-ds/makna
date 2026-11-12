-- G17 candidate active_contract_count, no time range.
-- Rules: a stock counted at the latest month end: one snapshot row per contract that still
-- owes principal at that date (the last snapshot_date).
SELECT COUNT(*) AS active_contract_count
FROM core.contract_monthly_snapshots AS s
WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
