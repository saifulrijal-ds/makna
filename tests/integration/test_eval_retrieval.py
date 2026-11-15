"""WP-15: the retrieval eval calls what the service calls (drift tests), isolates a search leg by
a zero weight, and runs end to end (acceptance 1, the shape of the report)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from evals import cli, gates, retrieval
from evals.golden.reference import QUESTIONS_FILE
from evals.golden.schema import load_questions
from evals.scores import LANGUAGES, SYSTEMS
from makna.config import load_search_runtime
from makna.model.governance import STRICT
from makna.model.search import SearchSettings
from makna.search.embedding import HashProvider
from makna.search.hybrid import HybridSearch
from makna.services import Caller, Services, compile_on_snapshot
from makna.services.snapshot import SnapshotCache

from . import service_support as sup

QUESTIONS = load_questions(QUESTIONS_FILE)
CONFIG = Path(__file__).resolve().parents[2] / "config" / "makna.toml"
QUERIES = [q.question for q in QUESTIONS[:12]] + ["NPF", "pembiayaan bermasalah", "xyzzy plugh"]

SETTINGS = {
    "defaults": SearchSettings(),
    "committed": load_search_runtime({}, CONFIG).search,
}


@pytest.fixture(params=sorted(SETTINGS))
def settings(request: pytest.FixtureRequest) -> SearchSettings:
    return SETTINGS[request.param]  # type: ignore[no-any-return]


@pytest.fixture
def world(service_world: tuple[Engine, Any]) -> Iterator[tuple[Engine, Any]]:
    yield service_world


# ---- the eval search is the service search ----


def test_the_eval_search_returns_the_ranked_ids_of_the_search_service(
    world: tuple[Engine, Any], settings: SearchSettings
) -> None:
    engine, users = world
    service = Services(engine, HashProvider(), policy=STRICT, search_settings=settings)
    searcher = HybridSearch(engine, HashProvider(), settings)
    callers: list[Caller] = [users.caller("viewer"), users.token("admin")]
    checked = 0
    for caller in callers:
        for query in QUERIES:
            served = service.search(
                caller, query, kinds=list(retrieval.KINDS), limit=retrieval.LIMIT
            )
            ours = retrieval.search_hits(searcher, query)
            assert [hit.id for hit in served.hits] == ours.ids, (caller.role, query)
            assert served.confidence == ours.confidence.band, query
            checked += 1
    assert checked == 2 * len(QUERIES)


def test_the_eval_query_is_stripped_as_the_service_strips_it(world: tuple[Engine, Any]) -> None:
    engine, users = world
    service = Services(engine, HashProvider(), policy=STRICT)
    searcher = HybridSearch(engine, HashProvider(), SearchSettings())
    padded = "  rasio kredit macet \n"
    served = service.search(
        users.caller("viewer"), padded, kinds=["metric", "glossary_term"], limit=10
    )
    assert [h.id for h in served.hits] == retrieval.search_hits(searcher, padded).ids


def test_the_eval_compile_returns_the_sql_of_the_compile_service(world: tuple[Engine, Any]) -> None:
    engine, users = world
    service = sup.services(engine)
    snapshot, _ = SnapshotCache(engine).current()
    requests = [
        {"metric": "npf_ratio"},
        {
            "metric": "disbursement_amount",
            "dimensions": ["branch.region_name"],
            "time_start": "2026-01-01",
            "time_end": "2026-07-01",
        },
        {
            "metric": "outstanding_principal",
            "grain": "quarter",
            "time_start": "2025-01-01",
            "time_end": "2026-01-01",
        },
        {"metric": "npf_ratio", "dimensions": ["customer.birth_year"]},  # masked personal
    ]
    from makna.model.request import QueryRequest

    for body in requests:
        served = service.compile_query(users.token("admin"), body)
        ours = compile_on_snapshot(
            snapshot, QueryRequest.model_validate(body), policy=STRICT, dialect="postgres"
        )
        assert served.sql == ours.sql
        assert served.model_dump() == ours.model_dump()


def test_the_eval_compile_refuses_what_the_compile_service_refuses(
    world: tuple[Engine, Any],
) -> None:
    from makna.model.request import QueryError, QueryRequest

    engine, users = world
    service = sup.services(engine)
    snapshot, _ = SnapshotCache(engine).current()
    for body in (
        {"metric": "write_off_amount"},  # a draft: unknown to a token
        {"metric": "npf_ratio", "dimensions": ["payment.payment_channel"]},
        {"metric": "npf_ratio", "dimensions": ["customer.national_id"]},
    ):
        with pytest.raises(QueryError) as served:
            service.compile_query(users.token("admin"), body)
        with pytest.raises(QueryError) as ours:
            compile_on_snapshot(
                snapshot, QueryRequest.model_validate(body), policy=STRICT, dialect="postgres"
            )
        assert (served.value.code, served.value.details) == (ours.value.code, ours.value.details)


# ---- a zero weight isolates a leg ----


def flat(settings: SearchSettings, **weights: float) -> SearchSettings:
    """Settings where only the two legs' weights matter: every status and kind weighs 1."""
    return settings.model_copy(
        update={
            "status_weights": {"approved": 1.0},
            "kind_weights": {k: 1.0 for k in settings.kind_weights},
            **weights,
        }
    )


@pytest.mark.parametrize("leg", ["keyword", "vector"])
def test_a_zero_weight_leaves_exactly_the_other_legs_ranking(
    world: tuple[Engine, Any], leg: str
) -> None:
    engine, _ = world
    other = "w_vector" if leg == "keyword" else "w_keyword"
    isolated = HybridSearch(engine, HashProvider(), flat(SearchSettings(), **{other: 0.0}))
    rank = "keyword_rank" if leg == "keyword" else "vector_rank"
    for query in QUERIES:
        hits = isolated.search(query, role="viewer", kinds=list(retrieval.KINDS), limit=10).hits
        ranks = [getattr(hit, rank) for hit in hits]
        # Only documents that the leg itself ranked are returned, in that leg's order.
        assert all(r is not None for r in ranks), query
        assert ranks == sorted(ranks), query
    with_kind_weights = retrieval.make_searchers(engine, HashProvider(), SearchSettings())
    legs = {"keyword": with_kind_weights.keyword, "vector": with_kind_weights.vector}
    for query in QUERIES:
        hits = retrieval.search_hits(legs[leg], query).hits
        assert all(getattr(hit, rank) is not None for hit in hits), query


def test_the_leg_searchers_differ_from_the_fused_one_only_by_a_weight(
    world: tuple[Engine, Any],
) -> None:
    engine, _ = world
    base = SearchSettings(w_keyword=3.0, w_vector=0.5, candidates=17)
    searchers = retrieval.make_searchers(engine, HashProvider(), base)
    assert searchers.fused._settings == base
    assert searchers.keyword._settings == base.model_copy(update={"w_vector": 0.0})
    assert searchers.vector._settings == base.model_copy(update={"w_keyword": 0.0})


# ---- an end-to-end run with the hash provider ----


def test_the_retrieval_eval_runs_and_reports_every_score(world: tuple[Engine, Any]) -> None:
    engine, _ = world
    searchers = retrieval.make_searchers(engine, HashProvider(), SearchSettings())
    section = retrieval.run_retrieval(engine, searchers, QUESTIONS, rounds=2)
    assert section["n_questions"] == len(QUESTIONS)
    assert len(section["questions"]) == len(QUESTIONS)
    assert section["latency"]["n"] == 2 * len(QUESTIONS)
    for system in SYSTEMS:
        scores = section["scores"][system]
        for name in ("recall_at_3", "recall_at_1", "mrr"):
            assert scores[name] is not None
            for language in LANGUAGES:
                assert name in scores["by_language"][language]
    fused = section["scores"]["fused"]
    assert fused["false_abstention_rate"] is not None
    assert fused["abstention_recall"] is not None
    first = section["questions"][0]
    assert set(first["rankings"]) == set(SYSTEMS) and first["confidence"] in (
        "high",
        "medium",
        "low",
    )


def database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def run_command(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    out: Path,
    gates_file: Path | None,
    command: str,
) -> int:
    monkeypatch.setenv("MAKNA_DATABASE_URL", database_url(engine))
    monkeypatch.setenv("MAKNA_EMBEDDING_PROVIDER", "hash")
    monkeypatch.delenv("MAKNA_ENGINE_URL", raising=False)
    argv = [command, "--out", str(out)]
    if gates_file is not None:
        argv += ["--gates", str(gates_file)]
    return cli.main(argv)


def write_gates(path: Path, **overrides: float) -> Path:
    text = gates.GATES_FILE.read_text()
    for key, value in overrides.items():
        line = next(ln for ln in text.splitlines() if ln.startswith(f"{key} ="))
        text = text.replace(line, f"{key} = {value}")
    path.write_text(text)
    return path


def test_the_retrieval_command_writes_both_reports_and_exits_by_the_gates(
    world: tuple[Engine, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, _ = world
    easy = write_gates(
        tmp_path / "easy.toml",
        recall_at_3_min=0.0,
        mrr_min=0.0,
        false_abstention_rate_max=1.0,
        indonesian_recall_gap_max=1.0,
        latency_p95_ms_below=100000,
    )
    out = tmp_path / "easy"
    assert run_command(monkeypatch, engine, out, easy, "retrieval") == 0
    document = json.loads((out / "report.json").read_text())
    assert (out / "report.md").read_text().startswith("# Makna eval report: PASS")
    assert document["command"] == "retrieval" and "compile" not in document
    assert document["settings"]["embedding"]["provider"] == "hash"
    assert document["settings"]["search"]["candidates"] == 30
    assert document["passed"] is True

    # The same run against a gate it cannot meet: exit 1, the gate is named.
    capsys.readouterr()
    hard = write_gates(tmp_path / "hard.toml", recall_at_3_min=1.01)
    assert run_command(monkeypatch, engine, tmp_path / "hard", hard, "retrieval") == 1
    assert "FAIL  Recall at 3 (fused)" in capsys.readouterr().out
    failed = json.loads((tmp_path / "hard" / "report.json").read_text())
    assert [g["name"] for g in failed["gates"] if not g["passed"]] == ["retrieval.recall_at_3_min"]
    assert (tmp_path / "hard" / "report.md").read_text().startswith("# Makna eval report: FAIL")


def test_an_unreachable_index_is_a_setup_problem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty_database: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sqlalchemy import create_engine

    from makna.index import migrate

    url = empty_database.render_as_string(hide_password=False)
    migrate.upgrade(url)
    engine = create_engine(empty_database)  # migrated, but nothing indexed
    try:
        code = run_command(monkeypatch, engine, tmp_path / "x", None, "retrieval")
    finally:
        engine.dispose()
    assert code == cli.EXIT_SETUP
    assert "makna index build" in capsys.readouterr().err
