"""Checks of search with the real embedding model (CI `stack` job; run inside the app container).

    docker compose exec -T app python -m deploy.check_search [--require-offline]

Proves WP-07 acceptance 8 (engine tier, real default model): the real model has 384 dimensions
and is deterministic; the index builds with it; "pembiayaan bermasalah" returns
`metric:npf_ratio` or `metric:npf_outstanding_principal` first; the 95th percentile of 200
searches is under 500 ms (NFR-2). With `--require-offline` it first proves that the container has
no route to the internet, which is how acceptance 9 (NFR-3) is shown: the index builds and
searches run with the model that the image carries.

It also prints, without asserting, how often each of the two search legs and the fused list put
the expected metric in the top 3 (the real-model measurement that `docs/notes/spike-environment-
limits.md` asks for; WP-15 tunes the weights on the golden set).
"""

from __future__ import annotations

import argparse
import socket
import statistics
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

from sqlalchemy import Engine

from makna.config import load_search_runtime
from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer
from makna.index.db import database_url_from_env, make_engine
from makna.model.search import DEFAULT_EMBEDDING_MODEL, EmbeddingProvider
from makna.search.embedding import make_provider
from makna.search.hybrid import HybridSearch

DEFINITIONS = "semantic/definitions"
SEARCHES = 200
P95_BUDGET_MS = 500.0
EXPECTED_DIMENSION = 384
FIRST = {"metric:npf_ratio", "metric:npf_outstanding_principal"}

# Bilingual queries the latency run cycles through, each with the metric it should find.
QUERIES: list[tuple[str, str]] = [
    ("NPF", "metric:npf_ratio"),
    ("kredit macet", "metric:npf_ratio"),
    ("non-performing financing ratio", "metric:npf_ratio"),
    ("pembiayaan bermasalah", "metric:npf_outstanding_principal"),
    ("Berapa total pencairan per wilayah tahun ini?", "metric:disbursement_amount"),
    ("how much did we disburse last month", "metric:disbursement_amount"),
    ("sisa pokok per cabang", "metric:outstanding_principal"),
    ("baki debet akhir bulan", "metric:outstanding_principal"),
    ("collection rate by region last quarter", "metric:collection_rate"),
    ("tingkat penagihan cabang bulan ini", "metric:collection_rate"),
    ("approval rate per product", "metric:approval_rate"),
    ("tingkat persetujuan aplikasi", "metric:approval_rate"),
    ("berapa nasabah baru bulan ini", "metric:new_customer_count"),
    ("jumlah kontrak aktif", "metric:active_contract_count"),
    ("pendapatan denda keterlambatan", "metric:penalty_income"),
    ("rasio janji bayar", "metric:promise_to_pay_rate"),
    ("PAR30 trend", "metric:par30_ratio"),
    ("average ticket size", "metric:average_ticket_size"),
    ("rasio restrukturisasi", "metric:restructured_ratio"),
    ("website conversion funnel", ""),
]


@dataclass(frozen=True)
class Suite:
    """One provider and one engine shared by the checks, so the model loads once."""

    provider: EmbeddingProvider
    engine: Engine
    fused: HybridSearch
    keyword: HybridSearch
    vector: HybridSearch


def make_real_provider() -> EmbeddingProvider:
    """The configured provider, which must be the real default model."""
    runtime = load_search_runtime()
    assert runtime.embedding_provider == "fastembed", (
        f"this check needs the real model; the provider is {runtime.embedding_provider}"
    )
    return make_provider("fastembed", runtime.embedding)


@cache
def suite() -> Suite:
    runtime = load_search_runtime()
    provider = make_real_provider()
    engine = make_engine(database_url_from_env())
    leg = runtime.search.model_copy
    return Suite(
        provider,
        engine,
        HybridSearch(engine, provider, runtime.search),
        HybridSearch(engine, provider, leg(update={"w_vector": 0.0})),
        HybridSearch(engine, provider, leg(update={"w_keyword": 0.0})),
    )


def check_offline() -> None:
    """No route to the internet: a connection to a public address must fail."""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 443), timeout=5).close()
        except OSError:
            continue
        raise AssertionError(f"the container can reach {host}: it is not offline")


def check_real_model() -> None:
    provider = suite().provider
    assert provider.model_id == DEFAULT_EMBEDDING_MODEL, provider.model_id
    assert provider.dimension == EXPECTED_DIMENSION, provider.dimension
    text = "Berapa rasio kredit macet bulan lalu?"
    first, second = provider.embed_query(text), provider.embed_query(text)
    assert len(first) == EXPECTED_DIMENSION, len(first)
    assert first == second, "the same text gave two different vectors"
    assert provider.embed_documents([text])[0] == first, "document and query vectors differ"
    assert any(x != 0 for x in first)


def check_index_builds_with_the_real_model() -> None:
    loaded = load_definitions(DEFINITIONS)
    assert not loaded.load_issues, loaded.load_issues
    result = indexer.build(suite().engine, suite().provider, definition_objects(loaded))
    # `init` has built the index with this model already: nothing is left to change.
    assert not result.reembedded_everything
    assert sum(result.by_kind.values()) > 150, result.by_kind
    print(f"     index: {result.by_kind}, changed this run: {result.total_changes}")


def check_pembiayaan_bermasalah() -> None:
    result = suite().fused.search("pembiayaan bermasalah", role="agent", limit=5)
    print(f"     first: {result.ids[:3]}")
    assert result.hits and result.hits[0].object_id in FIRST, result.ids


def check_latency() -> None:
    """The first search loads the model; it counts, as the 95th percentile hides one in 200."""
    fused = suite().fused
    times: list[float] = []
    for i in range(SEARCHES):
        query = QUERIES[i % len(QUERIES)][0]
        started = time.perf_counter()
        fused.search(query, role="agent", limit=5)
        times.append((time.perf_counter() - started) * 1000)
    ordered = sorted(times)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    print(
        f"     {SEARCHES} searches: first {times[0]:.0f} ms (loads the model), "
        f"p50 {statistics.median(ordered):.0f} ms, p95 {p95:.0f} ms, max {ordered[-1]:.0f} ms"
    )
    assert p95 < P95_BUDGET_MS, f"p95 {p95:.0f} ms is not under {P95_BUDGET_MS:.0f} ms (NFR-2)"


def report_legs() -> None:
    """Informative: recall at 1 and 3 of each leg alone and of the fused list."""
    fused, keyword, vector = suite().fused, suite().keyword, suite().vector
    answerable = [(q, e) for q, e in QUERIES if e]
    for label, search in (("keyword only", keyword), ("vector only", vector), ("fused", fused)):
        at1 = at3 = 0
        for query, expected in answerable:
            ids = search.search(query, role="agent", limit=3).ids
            at1 += ids[:1] == [expected]
            at3 += expected in ids
        n = len(answerable)
        print(f"     {label:12} recall@1 {at1 / n:.2f}  recall@3 {at3 / n:.2f}  ({n} queries)")
    for query, expected in QUERIES:
        if not expected:
            result = fused.search(query, role="agent")
            print(f"     no-answer {query!r}: confidence {result.confidence.band}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deploy.check_search", description=__doc__)
    parser.add_argument("--require-offline", action="store_true", help="first prove no internet")
    args = parser.parse_args(argv)
    checks: list[Callable[[], None]] = []
    if args.require_offline:
        checks.append(check_offline)
    checks += [
        check_latency,
        check_real_model,
        check_index_builds_with_the_real_model,
        check_pembiayaan_bermasalah,
        report_legs,
    ]
    failed = 0
    for check in checks:
        try:
            check()
        except Exception as error:
            failed += 1
            where = traceback.extract_tb(error.__traceback__)[-1]
            print(
                f"FAIL {check.__name__}: {type(error).__name__}: {error!r}"
                f" (at {where.filename}:{where.lineno}: {where.line})"
            )
        else:
            print(f"ok   {check.__name__}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
