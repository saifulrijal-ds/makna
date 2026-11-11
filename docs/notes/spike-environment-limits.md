The design spikes ran without Docker and without a real embedding model; the first packages that have them must re-run the proofs.

What was not available in the design session (2026-11-11): Docker, a Trino download, and model downloads from Hugging Face.

- Compiler: the five cases ran on DuckDB, and after D-22 also on a real PostgreSQL 16 server through `pgserver` (`spikes/compiler/run_on_postgres.py`, all pass). WP-11 must run them with the product compiler on the sample PostgreSQL database and record the result. Trino is put aside (D-22); the old Trino-dialect output in `spikes/compiler/output.txt` is history.
- Search: the vector leg used hashed character trigrams, not a language model. WP-07 and WP-15 must measure retrieval with the real multilingual model, in both languages, and report keyword-only, vector-only, and fused scores.
- `pgserver` (embedded PostgreSQL used by the spikes) needs Python 3.12 (no 3.13 wheel) and has no `pg_trgm`. It is a spike tool only; product tests use a real PostgreSQL with pgvector.
- For fast unit tests of the compiler, print the same sqlglot syntax tree for DuckDB and run it in-process. This is cheap and catches logic errors, but it does not replace the PostgreSQL engine tier.
