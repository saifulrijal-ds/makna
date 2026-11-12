-- G31 approval_rate by risk grade and channel, 2026-01-01 to 2026-04-01.
-- Rules: official date is the DECISION date (tgl_putusan); decided means approved or rejected
-- (withdrawn and in-process are in neither side); risk grade (grade_risiko) and channel
-- (saluran) are columns of the application itself; rate = approved / decided after counting.
WITH agg AS (
  SELECT
    a.grade_risiko AS risk_grade,
    a.saluran AS channel,
    COUNT(*) FILTER (WHERE a.sts_putusan = 'approved') AS approved_application_count,
    COUNT(*) AS decided_application_count
  FROM core.loan_applications AS a
  WHERE a.sts_putusan IN ('approved', 'rejected')
    AND a.tgl_putusan >= DATE '2026-01-01'
    AND a.tgl_putusan < DATE '2026-04-01'
  GROUP BY a.grade_risiko, a.saluran
)
SELECT
  risk_grade AS application__risk_grade,
  channel AS application__channel,
  approved_application_count,
  decided_application_count,
  CAST(approved_application_count AS DOUBLE PRECISION) / NULLIF(decided_application_count, 0)
    AS approval_rate
FROM agg
ORDER BY risk_grade COLLATE "C", channel COLLATE "C"
