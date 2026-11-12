-- G39 decided_application_count per month, 2025-10-01 to 2026-01-01.
-- Rules: official date is the DECISION date (tgl_putusan); decided means status approved or
-- rejected (withdrawn and in-process applications are excluded).
SELECT
  CAST(date_trunc('month', a.tgl_putusan) AS DATE) AS metric_time,
  COUNT(*) AS decided_application_count
FROM core.loan_applications AS a
WHERE a.sts_putusan IN ('approved', 'rejected')
  AND a.tgl_putusan >= DATE '2025-10-01'
  AND a.tgl_putusan < DATE '2026-01-01'
GROUP BY CAST(date_trunc('month', a.tgl_putusan) AS DATE)
ORDER BY metric_time
