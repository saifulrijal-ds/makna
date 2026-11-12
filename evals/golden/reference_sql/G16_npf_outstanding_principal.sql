-- G16 candidate npf_outstanding_principal, no time range.
-- Rules: balance at the latest month end (last snapshot_date); NPF means days_past_due > 90;
-- the amount of principal owed on those positions.
SELECT COALESCE(SUM(s.outstanding_principal), 0) AS npf_outstanding_principal
FROM core.contract_monthly_snapshots AS s
WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
  AND s.days_past_due > 90
