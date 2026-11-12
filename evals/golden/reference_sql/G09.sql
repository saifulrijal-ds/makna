-- G09 collection_rate, monthly, sharia products only, 2026-01-01 to 2026-07-01.
-- Rules: payments and installments are aggregated separately and combined with UNION ALL +
-- GROUP BY. Collected: posting_date, reversed payments excluded, principal + interest only.
-- Due: due_date, current schedule rows only. Month buckets use each input's own official
-- date. The product filter is the contract's product (through contracts -> products) on both
-- sides.
WITH collected AS (
  SELECT
    CAST(date_trunc('month', pay.posting_date) AS DATE) AS metric_time,
    SUM(pay.principal_paid + pay.interest_paid) AS collected_amount
  FROM core.payments AS pay
  LEFT JOIN core.contracts AS c ON c.contract_id = pay.contract_id
  LEFT JOIN core.products AS p ON p.product_id = c.product_id
  WHERE NOT pay.is_reversed
    AND pay.posting_date >= DATE '2026-01-01'
    AND pay.posting_date < DATE '2026-07-01'
    AND p.financing_scheme = 'sharia'
  GROUP BY CAST(date_trunc('month', pay.posting_date) AS DATE)
),
due AS (
  SELECT
    CAST(date_trunc('month', i.due_date) AS DATE) AS metric_time,
    SUM(i.installment_amount) AS due_amount
  FROM core.installment_schedules AS i
  LEFT JOIN core.contracts AS c ON c.contract_id = i.contract_id
  LEFT JOIN core.products AS p ON p.product_id = c.product_id
  WHERE i.is_current
    AND i.due_date >= DATE '2026-01-01'
    AND i.due_date < DATE '2026-07-01'
    AND p.financing_scheme = 'sharia'
  GROUP BY CAST(date_trunc('month', i.due_date) AS DATE)
),
combined AS (
  SELECT metric_time, collected_amount, CAST(NULL AS NUMERIC) AS due_amount FROM collected
  UNION ALL
  SELECT metric_time, CAST(NULL AS NUMERIC) AS collected_amount, due_amount FROM due
),
agg AS (
  SELECT
    metric_time,
    COALESCE(MAX(collected_amount), 0) AS collected_amount,
    COALESCE(MAX(due_amount), 0) AS due_amount
  FROM combined
  GROUP BY metric_time
)
SELECT
  metric_time,
  collected_amount,
  due_amount,
  CAST(collected_amount AS DOUBLE PRECISION) / NULLIF(due_amount, 0) AS collection_rate
FROM agg
ORDER BY metric_time
