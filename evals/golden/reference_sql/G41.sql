-- G41 due_amount, April 2026.
-- Rules: official date is due_date; only the CURRENT schedule (is_current), so installment
-- rows replaced by a restructuring are not counted twice; installment_amount is principal
-- plus interest.
SELECT SUM(i.installment_amount) AS due_amount
FROM core.installment_schedules AS i
WHERE i.is_current
  AND i.due_date >= DATE '2026-04-01'
  AND i.due_date < DATE '2026-05-01'
