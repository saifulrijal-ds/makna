-- G29 promise_to_pay_count, 2026-01-01 to 2026-07-01.
-- Rules: official date is the action date (tgl_aksi); only activities whose outcome (hasil)
-- is 'promise_to_pay'; counts activities, not customers.
SELECT COUNT(*) AS promise_to_pay_count
FROM core.collection_actions AS ca
WHERE ca.hasil = 'promise_to_pay'
  AND ca.tgl_aksi >= DATE '2026-01-01'
  AND ca.tgl_aksi < DATE '2026-07-01'
