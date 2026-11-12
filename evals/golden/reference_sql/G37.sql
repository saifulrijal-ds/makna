-- G37 disbursement_amount per month, 2025-07-01 to 2026-01-01.
-- Rules: official date is disbursement_date; cancelled contracts excluded; each row is the
-- calendar month of the disbursement date.
SELECT
  CAST(date_trunc('month', c.disbursement_date) AS DATE) AS metric_time,
  SUM(c.principal_amount) AS disbursement_amount
FROM core.contracts AS c
WHERE c.disbursement_date IS NOT NULL
  AND c.disbursement_date >= DATE '2025-07-01'
  AND c.disbursement_date < DATE '2026-01-01'
GROUP BY CAST(date_trunc('month', c.disbursement_date) AS DATE)
ORDER BY metric_time
