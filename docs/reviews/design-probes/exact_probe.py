import sys, tempfile
from pathlib import Path
import importlib.util
_spec = importlib.util.spec_from_file_location("search_spike", Path(__file__).resolve().parents[3] / "spikes/search/run_spike.py")
s = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(s)
s.ROOT = Path(__file__).resolve().parents[3]
import pgserver, psycopg
pg = pgserver.get_server(tempfile.mkdtemp())
with psycopg.connect(pg.get_uri(), autocommit=True) as conn, conn.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # untyped vector column, no vector index: the DR-9 / ARCHITECTURE 8.1 design
    cur.execute("""CREATE TABLE search_document (id text PRIMARY KEY, kind text NOT NULL, status text NOT NULL,
      names text NOT NULL, description text NOT NULL, extra text NOT NULL,
      tsv tsvector GENERATED ALWAYS AS (setweight(to_tsvector('simple', names), 'A') ||
        setweight(to_tsvector('simple', description), 'B') || setweight(to_tsvector('simple', extra), 'C')) STORED,
      embedding vector NOT NULL)""")
    cur.execute("CREATE INDEX ON search_document USING gin (tsv)")
    docs = s.corpus()
    for d in docs:
        names = " ; ".join(d["names"])
        cur.execute("INSERT INTO search_document (id, kind, status, names, description, extra, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s::vector)",
                    (d["id"], d["kind"], d["status"], names, d["description"], d["extra"], s.vec_literal(s.embed(f"{names} {names} {d['description']}"))))
    ans = [(q, e) for q, e in s.QUERIES if e]
    for label, wk, wv in [("keyword only", 1, 0), ("vector only*", 0, 1), ("hybrid (RRF)", 1, 1)]:
        r1 = r3 = mrr = 0.0
        for q, e in ans:
            ids = [r[0] for r in s.search(cur, q, w_kw=wk, w_vec=wv, k=10)]
            rank = ids.index(e) + 1 if e in ids else None
            r1 += rank == 1; r3 += bool(rank and rank <= 3); mrr += 1 / rank if rank else 0
        print(f"untyped vector, exact scan: {label:14} r@1 {r1/len(ans):.2f} r@3 {r3/len(ans):.2f} MRR {mrr/len(ans):.2f}")
    # agent view: approved only
    r3 = sum(1 for q, e in ans if e in [r[0] for r in s.search(cur, q, statuses=("approved",), k=10)][:3])
    print(f"hybrid, approved-only (agent view): r@3 {r3/len(ans):.2f}")
pg.cleanup()
