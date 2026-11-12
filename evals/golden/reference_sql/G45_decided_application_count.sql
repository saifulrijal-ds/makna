-- G45 candidate decided_application_count, 2026-04-01 to 2026-07-01.
-- Rules: official date is the DECISION date (tgl_putusan); decided means status approved or
-- rejected (withdrawn and in-process applications are excluded).
SELECT COUNT(*) AS decided_application_count
FROM core.loan_applications AS a
WHERE a.sts_putusan IN ('approved', 'rejected')
  AND a.tgl_putusan >= DATE '2026-04-01'
  AND a.tgl_putusan < DATE '2026-07-01'
