-- G16 candidate npf_outstanding_principal, no time range.
-- Rules: balance at the latest month end (last snapshot_date); NPF means days_past_due > 90;
-- the amount of principal owed on those positions.
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
SELECT COALESCE(SUM(s.outstanding_principal), 0) AS npf_outstanding_principal
FROM core.contract_monthly_snapshots AS s
WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
  AND COALESCE(s.days_past_due, 0) <= 210
  AND s.days_past_due > 90
