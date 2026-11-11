# Spikes

Small experiments that prove a design risk. They are evidence, not product code.
Do not import from `spikes/` in the product, and do not extend the spikes into
the product: write the real modules fresh, with tests, following
`docs/ARCHITECTURE.md`. Each spike keeps its captured output in `output.txt`.

| Spike | Question | Result |
|---|---|---|
| `compiler/` | Can a ratio metric with multi-hop joins, separate fact tables, and balance (semi-additive) measures be compiled to SQL with sqlglot from the YAML schema alone, with masking enforced? | Yes. 5 result checks, 9 governance checks, and 7 example requests pass. The 5 cases also pass on a real PostgreSQL 16 server and on DuckDB (`run_on_postgres.py`), the MVP target engines (D-22). |
| `search/` | Can keyword plus vector search with rank fusion, status rules, and low latency be done in one PostgreSQL statement? | Yes for the mechanics. Semantic quality with a real multilingual model, and reliable "no match" detection, are not proven. |
