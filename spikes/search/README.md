# Spike: hybrid search on PostgreSQL

**Question.** Can one PostgreSQL database (P-2) do keyword search, vector
search, rank fusion, and the status rules of SRC-3 in one statement, fast
enough for NFR-2?

**How to run** (Python 3.12; the embedded PostgreSQL wheel has no 3.13 build):

```
uv run -p 3.12 --with pgserver --with "psycopg[binary]" --with pyyaml --with numpy python spikes/search/run_spike.py
```

Tested on 2026-11-11 with PostgreSQL 16.2 and pgvector 0.6.2 (bundled in
`pgserver` 0.1.4). Captured output: `output.txt`.

**What it does.** It indexes 58 documents (the example definitions and their
dimensions, plus 13 more planned metrics with English and Indonesian synonyms,
one of them a draft and one deprecated) and runs 28 queries in both languages,
4 of which have no valid answer.

**What it proved.**

- One SQL statement with two CTEs (full-text and vector) fused by reciprocal rank fusion works, with status and kind weights applied in SQL. Recall at 3 is 1.00 on the 24 answerable queries.
- Full-text search with the `simple` configuration (no stemming), an OR query built from the query's words after removing English and Indonesian stop words, prefix matching for words of four letters or more, and synonyms stored in the highest-weight field, already finds 96% in the top 3 in both languages.
- Deprecated objects are hidden; drafts appear for web users and are excluded with an "approved only" filter for agents.
- Latency is under 1 ms per search at this size, so the 500 ms budget (NFR-2) will be spent on computing the query embedding, not on PostgreSQL.

**What it showed that changes the design.**

- Fusion can make the first result worse when one leg is weak: recall at 1 was 0.88 for keyword only and 0.75 for the fused list. The weights of the two legs must be configuration, tuned on the golden set, not constants.
- "No match" cannot be decided from scores alone. Generic words such as "rate" match many metrics, so "employee attrition rate" gets a keyword score higher than some valid queries. IDF-weighted coverage of the query by the names and synonyms of the top hits separates better (no-answer queries score at most 0.28; answerable ones have a median of 0.55) but still overlaps (minimum 0.23). The design therefore returns a confidence band with the matched terms as evidence, leaves the final decision to abstain to the caller, and measures abstention in the evals.

**What is not proven.**

- **Semantic quality.** The embedding in this spike is a stand-in (hashed character trigrams) because model downloads were blocked in the design environment. It tests the pgvector path and tolerance to typos, not meaning. Whether the default model (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`) handles Indonesian finance vocabulary well must be measured in WP-07 and WP-16.
- Embedding latency on CPU with the real model.
- `pg_trgm` was not available in the embedded server and is not part of the design.
