-- G12 average_ticket_size for sharia products, 2026-01-01 to 2026-07-01.
-- Rules: disbursed principal divided by the number of disbursed contracts, both on the
-- disbursement date; cancelled contracts (no disbursement date) are in neither side; the
-- scheme is the contract's product (contracts.product_id -> products.financing_scheme).
-- Both inputs come from the contracts table, so there is no join between fact tables.
WITH agg AS (
  SELECT
    COALESCE(SUM(c.principal_amount), 0) AS disbursement_amount,
    COUNT(*) AS disbursed_contract_count
  FROM core.contracts AS c
  LEFT JOIN core.products AS p ON p.product_id = c.product_id
  WHERE c.disbursement_date IS NOT NULL
    AND c.disbursement_date >= DATE '2026-01-01'
    AND c.disbursement_date < DATE '2026-07-01'
    AND p.financing_scheme = 'sharia'
)
SELECT
  disbursement_amount,
  disbursed_contract_count,
  CAST(disbursement_amount AS DOUBLE PRECISION) / NULLIF(disbursed_contract_count, 0)
    AS average_ticket_size
FROM agg
