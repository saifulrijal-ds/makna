-- G43 candidate collection_action_count, June 2026.
-- Rules: official date is the action date (collection_actions.tgl_aksi); counts activities.
SELECT COUNT(*) AS collection_action_count
FROM core.collection_actions AS ca
WHERE ca.tgl_aksi >= DATE '2026-06-01'
  AND ca.tgl_aksi < DATE '2026-07-01'
