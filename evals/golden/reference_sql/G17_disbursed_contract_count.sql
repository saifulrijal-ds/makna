-- G17 candidate disbursed_contract_count, no time range (all data).
-- Rules: a flow counted on disbursement_date; cancelled contracts (no disbursement date)
-- are not counted.
SELECT COUNT(*) AS disbursed_contract_count
FROM core.contracts AS c
WHERE c.disbursement_date IS NOT NULL
