"""Apply ARCHITECTURE.md 8.3 confidence rule (union coverage over top 3 names; high>=0.6, low<0.3)
to the search spike's corpus and queries. Also run once without the HNSW index (exact search, DR-9)."""
import math, sys, tempfile
from pathlib import Path
import importlib.util
_spec = importlib.util.spec_from_file_location("search_spike", Path(__file__).resolve().parents[3] / "spikes/search/run_spike.py")
s = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(s)
s.ROOT = Path(__file__).resolve().parents[3]
import pgserver, psycopg

def run(cur, docs, label, kinds=None, statuses=("approved",)):
    name_tokens = {d["id"]: set(s.tokens(" ".join(d["names"]))) for d in docs}
    df = {}
    for toks in name_tokens.values():
        for t in toks: df[t] = df.get(t, 0) + 1
    idf = lambda t: math.log((len(docs) + 1) / (df.get(t, 0) + 0.5))
    print(f"\n--- {label}")
    lows = []
    for q, exp_id in s.QUERIES:
        rows = s.search(cur, q, statuses=statuses, k=30)
        if kinds:
            rows = [r for r in rows if r[0].split(':')[0] in kinds]
        top = rows[:3]
        qt = s.tokens(q)
        hit = lambda t: any(any(n == t or (len(t) >= 4 and n.startswith(t)) for n in name_tokens[r[0]]) for r in top)
        cov = sum(idf(t) for t in qt if hit(t)) / sum(idf(t) for t in qt) if qt else 0
        band = "high" if cov >= 0.6 else ("low" if cov < 0.3 else "medium")
        unmatched = [t for t in qt if not hit(t)]
        print(f"  {q[:44]:44} cov={cov:4.2f} band={band:6} {'(no answer expected)' if exp_id is None else ''} unmatched={unmatched}")
        if exp_id is not None and band == "low": lows.append(q)
    n_ans = sum(1 for _, e in s.QUERIES if e)
    print(f"  answerable queries banded LOW (false abstention): {len(lows)}/{n_ans} = {len(lows)/n_ans:.2f}  {lows}")

pg = pgserver.get_server(tempfile.mkdtemp())
with psycopg.connect(pg.get_uri(), autocommit=True) as conn, conn.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute(f"""CREATE TABLE search_document (id text PRIMARY KEY, kind text NOT NULL, status text NOT NULL,
      names text NOT NULL, description text NOT NULL, extra text NOT NULL,
      tsv tsvector GENERATED ALWAYS AS (setweight(to_tsvector('simple', names), 'A') ||
        setweight(to_tsvector('simple', description), 'B') || setweight(to_tsvector('simple', extra), 'C')) STORED,
      embedding vector({s.DIM}) NOT NULL)""")
    docs = s.corpus()
    for d in docs:
        names = " ; ".join(d["names"])
        cur.execute("INSERT INTO search_document (id, kind, status, names, description, extra, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s::vector)",
                    (d["id"], d["kind"], d["status"], names, d["description"], d["extra"], s.vec_literal(s.embed(f"{names} {names} {d['description']}"))))
    # exact search (no HNSW index), as DR-9 specifies
    run(cur, docs, "agent default (approved only, all kinds), union coverage over top 3, exact vector search")
    run(cur, docs, "retrieval-eval setting (kinds metric+glossary_term, approved only)", kinds={"metric", "glossary_term"})
pg.cleanup()
