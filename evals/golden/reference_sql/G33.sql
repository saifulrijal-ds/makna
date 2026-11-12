-- G33 restructured_outstanding_principal by product category, no time range.
-- Rules: balance at the latest month end (last snapshot_date); restructured means the
-- CONTRACT has is_restructured = true (rule contract.restructured, a flag on contracts reached
-- through contract_monthly_snapshots.contract_id); product is the contract's product.
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
SELECT
  p.product_category AS product__product_category,
  COALESCE(SUM(s.outstanding_principal), 0) AS restructured_outstanding_principal
FROM core.contract_monthly_snapshots AS s
LEFT JOIN core.contracts AS c ON c.contract_id = s.contract_id
LEFT JOIN core.products AS p ON p.product_id = c.product_id
WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
  AND COALESCE(s.days_past_due, 0) <= 210
  AND c.is_restructured
GROUP BY p.product_category
ORDER BY p.product_category COLLATE "C"
