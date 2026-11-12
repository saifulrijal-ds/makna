-- G05 npf_ratio, monthly, car-backed products only, 2026-01-01 to 2026-07-01.
-- Rules: balance metric, month grain, so each month reports its own month-end snapshot
-- (there is exactly one snapshot date per month); NPF means days_past_due > 90; the product
-- filter is the contract's product (contract_monthly_snapshots -> contracts -> products).
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
WITH agg AS (
  SELECT
    CAST(date_trunc('month', s.snapshot_date) AS DATE) AS metric_time,
    COALESCE(SUM(s.outstanding_principal) FILTER (WHERE s.days_past_due > 90), 0)
      AS npf_outstanding_principal,
    COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
  FROM core.contract_monthly_snapshots AS s
  LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
  LEFT JOIN core.products AS p ON p.product_id = c.product_id
  WHERE s.snapshot_date >= DATE '2026-01-01'
    AND s.snapshot_date < DATE '2026-07-01'
    AND COALESCE(s.days_past_due, 0) <= 210
    AND p.product_category = 'car_backed'
  GROUP BY CAST(date_trunc('month', s.snapshot_date) AS DATE)
)
SELECT
  metric_time,
  npf_outstanding_principal,
  outstanding_principal,
  CAST(npf_outstanding_principal AS DOUBLE PRECISION) / NULLIF(outstanding_principal, 0)
    AS npf_ratio
FROM agg
ORDER BY metric_time
