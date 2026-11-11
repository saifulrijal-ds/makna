The design spikes ran without Trino and without a real embedding model; the first packages that have them must re-run the proofs.

What was not available in the design session (2026-11-11): Docker, a Trino download, and model downloads from Hugging Face.

- Compiler: the generated Trino SQL was executed on DuckDB (the exact text, with a database named `lending` and a schema named `core`) and re-parsed with sqlglot's Trino dialect. WP-11 must run the five cases in `spikes/compiler/run_spike.py` on a real Trino server and record the result.
- Search: the vector leg used hashed character trigrams, not a language model. WP-07 and WP-15 must measure retrieval with the real multilingual model, in both languages, and report keyword-only, vector-only, and fused scores.
- `pgserver` (embedded PostgreSQL used by the search spike) has no Python 3.13 wheel and no `pg_trgm`. It is a spike tool only; product tests use a real PostgreSQL with pgvector.
- For fast unit tests of the compiler, print the same sqlglot syntax tree for DuckDB and run it in-process. This is cheap and catches logic errors, but it does not replace the Trino tier.
