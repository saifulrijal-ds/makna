-- G27 approved_application_count, 2026-01-01 to 2026-04-01.
-- Rules: official date is the DECISION date (tgl_putusan), not the submission time; only
-- status 'approved' (an approved application does not have to become a contract).
SELECT COUNT(*) AS approved_application_count
FROM core.loan_applications AS a
WHERE a.sts_putusan = 'approved'
  AND a.tgl_putusan >= DATE '2026-01-01'
  AND a.tgl_putusan < DATE '2026-04-01'
