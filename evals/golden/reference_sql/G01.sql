-- G01 disbursement_amount, June 2026.
-- Rules: official date is contracts.disbursement_date; cancelled contracts have no
-- disbursement date and are excluded; range 2026-06-01 inclusive to 2026-07-01 exclusive.
-- No dimension, so one row.
SELECT SUM(c.principal_amount) AS disbursement_amount
FROM core.contracts AS c
WHERE c.disbursement_date IS NOT NULL
  AND c.disbursement_date >= DATE '2026-06-01'
  AND c.disbursement_date < DATE '2026-07-01'
