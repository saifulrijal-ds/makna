-- G21 application_count, March 2026.
-- Rules: official date is the SUBMISSION date, the DATE part of loan_applications.tgl_pengajuan
-- (a timestamp); every application counts whatever its outcome (approved, rejected,
-- cancelled, in process).
SELECT COUNT(*) AS application_count
FROM core.loan_applications AS a
WHERE CAST(a.tgl_pengajuan AS DATE) >= DATE '2026-03-01'
  AND CAST(a.tgl_pengajuan AS DATE) < DATE '2026-04-01'
