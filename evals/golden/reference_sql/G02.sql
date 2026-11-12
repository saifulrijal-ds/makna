-- G02 disbursement_amount by region, 2026-01-01 to 2026-07-01.
-- Rules: official date is contracts.disbursement_date (not signed_date); cancelled contracts
-- excluded (disbursement_date IS NOT NULL); region comes from the BOOKING branch
-- (contracts.booking_branch_id), the branch pinned for disbursement metrics.
SELECT
  b.region_name AS branch__region_name,
  SUM(c.principal_amount) AS disbursement_amount
FROM core.contracts AS c
LEFT JOIN core.branches AS b ON b.branch_id = c.booking_branch_id
WHERE c.disbursement_date IS NOT NULL
  AND c.disbursement_date >= DATE '2026-01-01'
  AND c.disbursement_date < DATE '2026-07-01'
GROUP BY b.region_name
ORDER BY b.region_name COLLATE "C"
