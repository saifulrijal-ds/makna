-- G15 collected_amount by payment channel, May 2026.
-- Rules: official date is posting_date (when the payment was booked), not payment_date;
-- reversed payments excluded; only principal_paid + interest_paid (penalties excluded).
-- The channel is a column of payments, so no join.
SELECT
  pay.payment_channel AS payment__payment_channel,
  SUM(pay.principal_paid + pay.interest_paid) AS collected_amount
FROM core.payments AS pay
WHERE NOT pay.is_reversed
  AND pay.posting_date >= DATE '2026-05-01'
  AND pay.posting_date < DATE '2026-06-01'
GROUP BY pay.payment_channel
ORDER BY pay.payment_channel COLLATE "C"
