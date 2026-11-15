Building the index without the `derive` hook drops the available dimensions stored on every metric; pass `make_derive(policy, dialect)` to every `indexer.build`.

Found in WP-15 (2026-11-15): `deploy/check_search.py` re-ran `indexer.build` with no hook right after `init`, and "changed this run: 24" was the 24 metrics losing `semantic_object.derived`. Search did not notice. The compile evals (part B) read those facts and refuse to start without them, and `get_definition` falls back to computing them from the snapshot.

- `makna index build`, `deploy.init`, and `deploy/check_search.py` pass the hook; a test (`tests/integration/test_check_search.py`) fails if the check drops the facts again.
- A report of "objects changed" on an index that should be up to date is the sign.
