Write column ordinals into a catalog export, and make an unchanged sync write nothing at all when `init` runs it.

Seen while building WP-06 (2026-11-11).

- If a file leaves out `ordinal`, the connector numbers columns by list position. Removing one column then shifts every later column, so a sync reports many "changed" columns besides the one removed. The sample export therefore carries explicit ordinals.
- `deploy/check_stack.py` proves "a second `init` changes nothing" by comparing PostgreSQL tuple counters (`n_tup_ins`, `n_tup_upd`) of the `makna` database. A `sync_run` row per run, or a `last_sync_at` update, would fail it. `sync_catalog(..., record_unchanged=False)` reads, finds no difference, and writes nothing; the `makna catalog sync` command keeps the default and records every run.
