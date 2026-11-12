-- G16 candidate npf_ratio, no time range.
-- Rules: balance at the latest month end (last snapshot_date); NPF means days_past_due > 90;
-- ratio of non-performing principal to all outstanding principal.
-- In the book (rule in_book): the write-off position, days_past_due > 210, is left out. A
-- contract is written off AT the month end of that position, so it is still a snapshot row.
WITH agg AS (
  SELECT
    COALESCE(SUM(s.outstanding_principal) FILTER (WHERE s.days_past_due > 90), 0)
      AS npf_outstanding_principal,
    COALESCE(SUM(s.outstanding_principal), 0) AS outstanding_principal
  FROM core.contract_monthly_snapshots AS s
  WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM core.contract_monthly_snapshots)
    AND COALESCE(s.days_past_due, 0) <= 210
)
SELECT
  npf_outstanding_principal,
  outstanding_principal,
  CAST(npf_outstanding_principal AS DOUBLE PRECISION) / NULLIF(outstanding_principal, 0)
    AS npf_ratio
FROM agg
