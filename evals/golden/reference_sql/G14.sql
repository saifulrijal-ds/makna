-- G14 disbursement_amount by branch, June 2026.
-- Rules: official date is disbursement_date; cancelled contracts excluded; "branch" means the
-- BOOKING branch (contracts.booking_branch_id), not the servicing, home, or application branch.
SELECT
  b.branch_name AS branch__branch_name,
  SUM(c.principal_amount) AS disbursement_amount
FROM core.contracts AS c
LEFT JOIN core.branches AS b ON b.branch_id = c.booking_branch_id
WHERE c.disbursement_date IS NOT NULL
  AND c.disbursement_date >= DATE '2026-06-01'
  AND c.disbursement_date < DATE '2026-07-01'
GROUP BY b.branch_name
ORDER BY b.branch_name COLLATE "C"
