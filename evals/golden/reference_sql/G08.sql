-- G08 collection_rate by region, 2026-04-01 to 2026-07-01 (last quarter, no grain).
-- Rules: two fact tables, so each input is aggregated on its own and the results are combined
-- with UNION ALL + GROUP BY (never joined row to row).
--   collected_amount: payments, official date posting_date, reversed payments excluded,
--     principal_paid + interest_paid (penalties excluded).
--   due_amount: installment_schedules, official date due_date, current schedule rows only
--     (is_current), installment_amount.
-- Region comes from the SERVICING branch through the contract for both inputs.
WITH collected AS (
  SELECT b.region_name AS region_name, SUM(pay.principal_paid + pay.interest_paid) AS collected_amount
  FROM core.payments AS pay
  LEFT JOIN core.contracts AS c ON c.contract_id = pay.contract_id
  LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
  WHERE NOT pay.is_reversed
    AND pay.posting_date >= DATE '2026-04-01'
    AND pay.posting_date < DATE '2026-07-01'
  GROUP BY b.region_name
),
due AS (
  SELECT b.region_name AS region_name, SUM(i.installment_amount) AS due_amount
  FROM core.installment_schedules AS i
  LEFT JOIN core.contracts AS c ON c.contract_id = i.contract_id
  LEFT JOIN core.branches AS b ON b.branch_id = c.servicing_branch_id
  WHERE i.is_current
    AND i.due_date >= DATE '2026-04-01'
    AND i.due_date < DATE '2026-07-01'
  GROUP BY b.region_name
),
combined AS (
  SELECT region_name, collected_amount, CAST(NULL AS NUMERIC) AS due_amount FROM collected
  UNION ALL
  SELECT region_name, CAST(NULL AS NUMERIC) AS collected_amount, due_amount FROM due
),
agg AS (
  SELECT
    region_name,
    COALESCE(MAX(collected_amount), 0) AS collected_amount,
    COALESCE(MAX(due_amount), 0) AS due_amount
  FROM combined
  GROUP BY region_name
)
SELECT
  region_name AS branch__region_name,
  collected_amount,
  due_amount,
  CAST(collected_amount AS DOUBLE PRECISION) / NULLIF(due_amount, 0) AS collection_rate
FROM agg
ORDER BY region_name COLLATE "C"
