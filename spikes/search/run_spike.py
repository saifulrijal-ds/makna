"""Spike: hybrid search (keyword + vector, fused with reciprocal rank fusion)
in one PostgreSQL statement, with status weighting and a no-match signal.

Runs on an embedded PostgreSQL 16 with pgvector (the `pgserver` wheel), so no
Docker is needed. The embedding here is a STAND-IN (hashed character
trigrams): it exercises the pgvector SQL path and fuzzy matching, but it does
not understand meaning. Real multilingual quality must be measured with the
real model in the eval work package (see README.md).

Usage (Python 3.12; pgserver has no 3.13 wheel):
  uv run -p 3.12 --with pgserver --with "psycopg[binary]" --with pyyaml --with numpy python spikes/search/run_spike.py
"""
from __future__ import annotations

import glob
import hashlib
import math
import re
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np
import pgserver
import psycopg
import yaml

ROOT = Path(__file__).resolve().parents[2]
DIM = 256
STOP = set("""a an the of is are was were what which who how much many do does did we our us in on at by for to from and or
per with show me give tell please this that last current total number amount
apa berapa yang di ke dari dan atau untuk dengan pada per kita kami saya tolong tampilkan lihat jumlah total ini itu adalah
bagaimana mana""".split())

# Metrics planned for the sample domain that are not among the schema examples.
# (name, label, description, en synonyms, id synonyms, status)
EXTRA = [
    ("application_count", "Applications Received", "Number of financing applications submitted in a period.", ["applications", "incoming applications", "leads submitted"], ["jumlah aplikasi", "pengajuan masuk", "aplikasi masuk"], "approved"),
    ("approval_rate", "Approval Rate", "Share of decided applications that were approved.", ["approval ratio", "acceptance rate"], ["tingkat persetujuan", "rasio approval", "persentase disetujui"], "approved"),
    ("disbursed_contract_count", "Contracts Disbursed", "Number of contracts whose funds were released in a period.", ["new contracts", "bookings", "units disbursed"], ["jumlah kontrak cair", "kontrak baru", "unit pencairan"], "approved"),
    ("average_ticket_size", "Average Ticket Size", "Average principal per disbursed contract.", ["average loan size", "ticket size"], ["rata-rata pencairan", "rata-rata plafon"], "approved"),
    ("active_contract_count", "Active Contracts", "Number of contracts with principal still owed at a month end.", ["active accounts", "live contracts", "number of loans"], ["kontrak aktif", "jumlah kontrak berjalan", "NOA"], "approved"),
    ("par30_ratio", "Portfolio at Risk over 30 Days", "Share of outstanding principal on contracts more than 30 days past due.", ["PAR30", "portfolio at risk", "delinquency ratio"], ["rasio tunggakan 30 hari", "PAR 30"], "approved"),
    ("restructured_ratio", "Restructured Share", "Share of outstanding principal on restructured contracts.", ["restructured portfolio share", "restructuring ratio"], ["rasio restrukturisasi", "porsi restru"], "approved"),
    ("penalty_income", "Late Payment Penalties Collected", "Late payment penalties received from customers.", ["late fees", "penalty", "late charges"], ["denda", "denda keterlambatan", "pendapatan denda"], "approved"),
    ("collection_action_count", "Collection Actions", "Number of collection activities such as calls, visits, and letters.", ["collection activities", "collection attempts"], ["aktivitas penagihan", "jumlah kunjungan dan telepon penagihan"], "approved"),
    ("promise_to_pay_rate", "Promise-to-Pay Rate", "Share of collection actions that ended with a promise to pay.", ["PTP rate", "promise to pay"], ["janji bayar", "rasio janji bayar", "PTP"], "approved"),
    ("new_customer_count", "New Customers", "Number of customers registered in a period.", ["customer acquisition", "new borrowers"], ["nasabah baru", "konsumen baru", "akuisisi nasabah"], "approved"),
    ("write_off_amount", "Write-Off Amount", "Principal written off in a period. Draft definition, not yet approved.", ["write-off", "charge-off", "bad debt written off"], ["hapus buku", "penghapusan piutang"], "draft"),
    ("npl_ratio", "NPL Ratio (old)", "Old non-performing loan ratio based on contract counts. Replaced by NPF Ratio.", ["NPL", "non-performing loan ratio"], ["rasio NPL", "kredit macet"], "deprecated"),
]

# (query, expected object id or None when no governed answer exists)
QUERIES = [
    ("What is our NPF?", "metric:npf_ratio"),
    ("berapa rasio kredit macet bulan lalu", "metric:npf_ratio"),
    ("non performing financing ratio by region", "metric:npf_ratio"),
    ("nominal pembiayaan bermasalah", "metric:npf_outstanding_principal"),
    ("total pencairan per wilayah tahun ini", "metric:disbursement_amount"),
    ("how much did we disburse last month", "metric:disbursement_amount"),
    ("realisasi pembiayaan Q1", "metric:disbursement_amount"),
    ("sisa pokok per cabang", "metric:outstanding_principal"),
    ("outstanding by dpd bucket", "metric:outstanding_principal"),
    ("baki debet akhir bulan", "metric:outstanding_principal"),
    ("collection rate by region last quarter", "metric:collection_rate"),
    ("tingkat penagihan cabang bulan ini", "metric:collection_rate"),
    ("berapa tagihan yang jatuh tempo bulan depan", "metric:due_amount"),
    ("cash collected in May by payment channel", "metric:collected_amount"),
    ("approval rate per product", "metric:approval_rate"),
    ("tingkat persetujuan aplikasi", "metric:approval_rate"),
    ("berapa nasabah baru bulan ini", "metric:new_customer_count"),
    ("jumlah kontrak aktif", "metric:active_contract_count"),
    ("average ticket size for car backed financing", "metric:average_ticket_size"),
    ("pendapatan denda keterlambatan", "metric:penalty_income"),
    ("rasio janji bayar collector", "metric:promise_to_pay_rate"),
    ("PAR30 trend", "metric:par30_ratio"),
    ("disbursment amout by region", "metric:disbursement_amount"),  # typos
    ("apa itu kolektibilitas", "dimension:contract_snapshot.collectibility"),
    ("employee attrition rate", None),
    ("berapa harga saham kita hari ini", None),
    ("website conversion funnel", None),
    ("net promoter score", None),
]


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP]


def embed(text: str) -> np.ndarray:
    """Stand-in embedding: hashed character trigrams, L2-normalised."""
    v = np.zeros(DIM, dtype=np.float32)
    for tok in tokens(text):
        padded = f"  {tok} "
        for i in range(len(padded) - 2):
            h = int.from_bytes(hashlib.blake2b(padded[i : i + 3].encode(), digest_size=4).digest(), "little")
            v[h % DIM] += 1.0
    n = np.linalg.norm(v)
    return v / n if n else v


def vec_literal(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


def syn(d: dict) -> list[str]:
    return [s for terms in (d.get("synonyms") or {}).values() for s in terms]


def corpus() -> list[dict]:
    docs = []
    for f in sorted(glob.glob(str(ROOT / "semantic/schema/examples/**/*.yaml"), recursive=True)):
        d = yaml.safe_load(Path(f).read_text())
        kind = d["kind"]
        docs.append({"id": f"{kind}:{d['name']}", "kind": kind, "status": d["status"], "names": [d["name"].replace("_", " "), d["label"], *syn(d)], "description": d["description"], "extra": " ".join(d.get("example_questions", []))})
        for dim in d.get("dimensions", []):
            docs.append({"id": f"dimension:{d['name']}.{dim['name']}", "kind": "dimension", "status": d["status"], "names": [dim["name"].replace("_", " "), *syn(dim)], "description": dim["description"], "extra": " ".join(str(v.get("description", "")) for v in dim.get("values", []))})
    for name, label, desc, en, idn, status in EXTRA:
        docs.append({"id": f"metric:{name}", "kind": "metric", "status": status, "names": [name.replace("_", " "), label, *en, *idn], "description": desc, "extra": ""})
    return docs


SEARCH_SQL = """
WITH q AS (SELECT to_tsquery('simple', %(tsq)s) AS tsq, %(vec)s::vector AS vec),
kw AS (
  SELECT d.id, ts_rank_cd(d.tsv, q.tsq) AS kw_score,
         row_number() OVER (ORDER BY ts_rank_cd(d.tsv, q.tsq) DESC, d.id) AS rnk
  FROM search_document d, q
  WHERE d.tsv @@ q.tsq AND d.status = ANY(%(statuses)s)
  ORDER BY kw_score DESC LIMIT 30),
vs AS (
  SELECT d.id, 1 - (d.embedding <=> q.vec) AS cos,
         row_number() OVER (ORDER BY d.embedding <=> q.vec, d.id) AS rnk
  FROM search_document d, q
  WHERE d.status = ANY(%(statuses)s)
  ORDER BY d.embedding <=> q.vec LIMIT 30)
SELECT d.id, d.status,
       (coalesce(%(w_kw)s / (60.0 + kw.rnk), 0) + coalesce(%(w_vec)s / (60.0 + vs.rnk), 0))
         * CASE d.status WHEN 'approved' THEN 1.0 ELSE 0.5 END
         * CASE d.kind WHEN 'metric' THEN 1.0 WHEN 'glossary_term' THEN 0.9 ELSE 0.8 END AS score,
       kw.kw_score, vs.cos
FROM search_document d
LEFT JOIN kw ON kw.id = d.id
LEFT JOIN vs ON vs.id = d.id
WHERE kw.id IS NOT NULL OR vs.id IS NOT NULL
ORDER BY score DESC, d.id
LIMIT %(k)s
"""


def search(cur, query: str, *, w_kw=1.0, w_vec=1.0, statuses=("approved", "draft", "in_review"), k=5):
    toks = tokens(query)
    tsq = " | ".join(f"{t}:*" if len(t) >= 4 else t for t in toks) or "zzzzzz"
    cur.execute(SEARCH_SQL, {"tsq": tsq, "vec": vec_literal(embed(query)), "w_kw": w_kw, "w_vec": w_vec, "statuses": list(statuses), "k": k})
    return cur.fetchall()


def main() -> int:
    pg = pgserver.get_server(tempfile.mkdtemp())
    with psycopg.connect(pg.get_uri(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f"""
            CREATE TABLE search_document (
              id text PRIMARY KEY, kind text NOT NULL, status text NOT NULL,
              names text NOT NULL, description text NOT NULL, extra text NOT NULL,
              tsv tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('simple', names), 'A') ||
                setweight(to_tsvector('simple', description), 'B') ||
                setweight(to_tsvector('simple', extra), 'C')) STORED,
              embedding vector({DIM}) NOT NULL)""")
        cur.execute("CREATE INDEX ON search_document USING gin (tsv)")
        docs = corpus()
        for d in docs:
            names = " ; ".join(d["names"])
            # names are repeated in the embedded text so synonyms dominate the vector
            cur.execute("INSERT INTO search_document (id, kind, status, names, description, extra, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s::vector)",
                        (d["id"], d["kind"], d["status"], names, d["description"], d["extra"], vec_literal(embed(f"{names} {names} {d['description']}"))))
        cur.execute("CREATE INDEX ON search_document USING hnsw (embedding vector_cosine_ops)")
        print(f"corpus: {len(docs)} documents; pgvector {cur.execute('SELECT extversion FROM pg_extension WHERE extname = %s', ('vector',)).fetchone()[0]}; {cur.execute('SHOW server_version').fetchone()[0]}")

        answerable = [(q, e) for q, e in QUERIES if e]
        print("\nleg            recall@1  recall@3  MRR")
        for label, w_kw, w_vec in [("keyword only", 1, 0), ("vector only*", 0, 1), ("hybrid (RRF)", 1, 1)]:
            r1 = r3 = mrr = 0.0
            for q, expected in answerable:
                ids = [r[0] for r in search(cur, q, w_kw=w_kw, w_vec=w_vec, k=10)]
                rank = ids.index(expected) + 1 if expected in ids else None
                r1 += rank == 1
                r3 += bool(rank and rank <= 3)
                mrr += 1 / rank if rank else 0
            n = len(answerable)
            print(f"{label:14} {r1 / n:8.2f} {r3 / n:9.2f} {mrr / n:6.2f}")
        print("* stand-in trigram vectors, not a language model")

        print("\nhybrid misses (expected not in top 3):")
        for q, expected in answerable:
            ids = [r[0] for r in search(cur, q, k=10)]
            if expected not in ids[:3]:
                print(f"  {q!r}: wanted {expected}, got {ids[:3]}")

        print("\nstatus rules:")
        rows = search(cur, "kredit macet")
        print("  'kredit macet' ->", [(r[0], r[1]) for r in rows[:3]])
        assert rows[0][0] == "metric:npf_ratio" and all(r[1] != "deprecated" for r in rows), "deprecated must be hidden, approved first"
        rows = search(cur, "write off hapus buku")
        print("  'write off hapus buku' (web default, drafts shown) ->", [(r[0], r[1]) for r in rows[:2]])
        rows = search(cur, "write off hapus buku", statuses=("approved",))
        print("  same query, approved only (agent default) ->", [(r[0], r[1]) for r in rows[:2]])
        assert all(r[1] == "approved" for r in rows)

        # No-match signal. Raw keyword scores do not separate "nothing fits" from a
        # weak but valid hit, because generic words such as "rate" match many
        # metrics. IDF-weighted coverage does better: how much of the query's
        # informative vocabulary appears in the names and synonyms of the top hit.
        name_tokens = {d["id"]: set(tokens(" ".join(d["names"]))) for d in docs}
        df: dict[str, int] = {}
        for toks in name_tokens.values():
            for t in toks:
                df[t] = df.get(t, 0) + 1

        def idf(t: str) -> float:
            return math.log((len(docs) + 1) / (df.get(t, 0) + 0.5))

        def coverage(query: str, doc_id: str) -> float:
            q = tokens(query)
            if not q:
                return 0.0
            hit = lambda t: any(n == t or (len(t) >= 4 and n.startswith(t)) for n in name_tokens[doc_id])  # noqa: E731
            return sum(idf(t) for t in q if hit(t)) / sum(idf(t) for t in q)

        print("\nno-match signal: IDF-weighted coverage of the query by the best of the top 3 hits' names and synonyms")
        print(f"  {'query':46} {'top hit':38} {'kw':>5} {'cos':>5} {'cover':>5}")
        cov_ok, cov_none = [], []
        for q, expected in QUERIES:
            rows = search(cur, q)
            kw = max((r[3] or 0) for r in rows) if rows else 0
            cos = max((r[4] or 0) for r in rows) if rows else 0
            cov = max((coverage(q, r[0]) for r in rows[:3]), default=0.0)
            (cov_none if expected is None else cov_ok).append(cov)
            print(f"  {q[:46]:46} {(rows[0][0] if rows else '-')[:38]:38} {kw:5.2f} {cos:5.2f} {cov:5.2f}  {'(no answer expected)' if expected is None else ''}")
        print(f"  coverage, answerable: min {min(cov_ok):.2f}, median {statistics.median(cov_ok):.2f}; no-answer: max {max(cov_none):.2f}")

        times = []
        for i in range(300):
            q = QUERIES[i % len(QUERIES)][0]
            t = time.perf_counter()
            search(cur, q)
            times.append((time.perf_counter() - t) * 1000)
        times.sort()
        print(f"\nlatency over 300 searches incl. stand-in embedding: p50 {statistics.median(times):.1f} ms, p95 {times[int(len(times) * 0.95)]:.1f} ms")
    pg.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
