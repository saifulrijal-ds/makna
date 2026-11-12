-- G43 candidate collected_amount, June 2026.
-- Rules: official date is posting_date; reversed payments excluded; principal + interest only
-- (penalties excluded).
SELECT SUM(pay.principal_paid + pay.interest_paid) AS collected_amount
FROM core.payments AS pay
WHERE NOT pay.is_reversed
  AND pay.posting_date >= DATE '2026-06-01'
  AND pay.posting_date < DATE '2026-07-01'
