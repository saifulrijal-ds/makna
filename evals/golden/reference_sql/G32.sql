-- G32 disbursement_amount by region for unsecured products, 2026-01-01 to 2026-07-01.
-- Rules: official date is disbursement_date; cancelled contracts excluded; region from the
-- BOOKING branch; the product filter is the contract's product category.
SELECT
  b.region_name AS branch__region_name,
  SUM(c.principal_amount) AS disbursement_amount
FROM core.contracts AS c
LEFT JOIN core.branches AS b ON b.branch_id = c.booking_branch_id
LEFT JOIN core.products AS p ON p.product_id = c.product_id
WHERE c.disbursement_date IS NOT NULL
  AND c.disbursement_date >= DATE '2026-01-01'
  AND c.disbursement_date < DATE '2026-07-01'
  AND p.product_category = 'unsecured'
GROUP BY b.region_name
ORDER BY b.region_name COLLATE "C"
