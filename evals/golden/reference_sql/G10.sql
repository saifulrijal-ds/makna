-- G10 approval_rate by product category, 2026-01-01 to 2026-04-01.
-- Rules: official date is the DECISION date (tgl_putusan), not the submission time; decided
-- means status approved or rejected (withdrawn and in-process applications are in neither
-- side); approval rate = approved / decided, computed after counting; product is the
-- application's own product (loan_applications.prod_id -> products).
WITH agg AS (
  SELECT
    p.product_category AS product_category,
    COUNT(*) FILTER (WHERE a.sts_putusan = 'approved') AS approved_application_count,
    COUNT(*) AS decided_application_count
  FROM core.loan_applications AS a
  LEFT JOIN core.products AS p ON p.product_id = a.prod_id
  WHERE a.sts_putusan IN ('approved', 'rejected')
    AND a.tgl_putusan >= DATE '2026-01-01'
    AND a.tgl_putusan < DATE '2026-04-01'
  GROUP BY p.product_category
)
SELECT
  product_category AS product__product_category,
  approved_application_count,
  decided_application_count,
  CAST(approved_application_count AS DOUBLE PRECISION) / NULLIF(decided_application_count, 0)
    AS approval_rate
FROM agg
ORDER BY product_category COLLATE "C"
