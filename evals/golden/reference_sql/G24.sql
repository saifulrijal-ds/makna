-- G24 disbursed_contract_count, 2025-01-01 to 2026-01-01.
-- Rules: counted on disbursement_date (not signed_date); cancelled contracts, which have no
-- disbursement date, are not counted.
SELECT COUNT(*) AS disbursed_contract_count
FROM core.contracts AS c
WHERE c.disbursement_date IS NOT NULL
  AND c.disbursement_date >= DATE '2025-01-01'
  AND c.disbursement_date < DATE '2026-01-01'
