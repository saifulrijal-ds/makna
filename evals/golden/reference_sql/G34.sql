-- G34 active_contract_count by customer segment at the end of 2025 (2025-12-01 to 2026-01-01).
-- Rules: a stock counted at the last month end in the range (2025-12-31), one snapshot row
-- per contract that still owes principal; the segment is the contract's own customer
-- (contracts.customer_id -> customers.customer_segment), the direct one-step path.
SELECT
  cu.customer_segment AS customer__customer_segment,
  COUNT(*) AS active_contract_count
FROM core.contract_monthly_snapshots AS s
LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
LEFT JOIN core.customers AS cu ON cu.customer_id = c.customer_id
WHERE s.snapshot_date = DATE '2025-12-31'
GROUP BY cu.customer_segment
ORDER BY cu.customer_segment COLLATE "C"
