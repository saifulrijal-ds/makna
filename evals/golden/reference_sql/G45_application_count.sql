-- G45 candidate application_count, 2026-04-01 to 2026-07-01.
-- Rules: official date is the SUBMISSION date (DATE part of tgl_pengajuan); every application
-- counts whatever its outcome.
SELECT COUNT(*) AS application_count
FROM core.loan_applications AS a
WHERE CAST(a.tgl_pengajuan AS DATE) >= DATE '2026-04-01'
  AND CAST(a.tgl_pengajuan AS DATE) < DATE '2026-07-01'
