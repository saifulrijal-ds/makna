-- G22 penalty_income, 2026-04-01 to 2026-07-01.
-- Rules: official date is posting_date; reversed payments excluded; only penalty_paid
-- (principal and interest are not penalties).
SELECT SUM(pay.penalty_paid) AS penalty_income
FROM core.payments AS pay
WHERE NOT pay.is_reversed
  AND pay.posting_date >= DATE '2026-04-01'
  AND pay.posting_date < DATE '2026-07-01'
