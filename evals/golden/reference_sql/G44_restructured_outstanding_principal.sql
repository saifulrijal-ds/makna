-- G44 candidate restructured_outstanding_principal, June 2026.
-- Rules: balance at the last month end in the range (2026-06-30); restructured means the
-- CONTRACT has is_restructured = true, reached through contract_monthly_snapshots.contract_id.
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
SELECT COALESCE(SUM(s.outstanding_principal), 0) AS restructured_outstanding_principal
FROM core.contract_monthly_snapshots AS s
LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
WHERE s.snapshot_date = DATE '2026-06-30'
  AND COALESCE(s.days_past_due, 0) <= 210
  AND c.is_restructured
