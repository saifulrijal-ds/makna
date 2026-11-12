-- G11 new_customer_count, June 2026.
-- Rules: official date is the registration date, the DATE part of customers.registered_at;
-- a customer is counted once, when the record was created, whether or not a contract follows.
SELECT COUNT(*) AS new_customer_count
FROM core.customers AS cu
WHERE CAST(cu.registered_at AS DATE) >= DATE '2026-06-01'
  AND CAST(cu.registered_at AS DATE) < DATE '2026-07-01'
