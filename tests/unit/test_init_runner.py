"""WP-05 criterion 4 (unit part): `init` runs ordered steps, stops at a failure, reports each."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from deploy import init
from deploy.init import Context, Step, run_steps


def context(tmp_path: Path) -> Context:
    return Context(
        data_dir=tmp_path,
        seed=1,
        scale=0.05,
        database_url="postgresql://unused",
        lending_loader_url="postgresql://unused",
    )


def test_steps_run_in_order_and_each_outcome_is_printed(tmp_path: Path) -> None:
    calls: list[str] = []

    def make(name: str) -> Step:
        def run(_c: Context) -> str:
            calls.append(name)
            return "done"

        return Step(name, run)

    out = io.StringIO()
    assert run_steps([make("a"), make("b"), make("c")], context(tmp_path), out) == 0
    assert calls == ["a", "b", "c"]
    assert out.getvalue().splitlines() == ["init: a: done", "init: b: done", "init: c: done"]


def test_a_failing_step_stops_the_run_and_exits_1(tmp_path: Path) -> None:
    calls: list[str] = []

    def boom(_c: Context) -> str:
        raise RuntimeError("no database")

    def record(name: str) -> Step:
        def run(_c: Context) -> str:
            calls.append(name)
            return "done"

        return Step(name, run)

    steps = [record("a"), Step("b", boom), record("c")]
    out = io.StringIO()
    assert run_steps(steps, context(tmp_path), out) == 1
    assert calls == ["a"]
    assert "init: b: FAILED: no database" in out.getvalue()


def test_the_step_list_starts_with_the_wp05_steps_in_dependency_order() -> None:
    names = [s.name for s in init.STEPS]
    assert names[:3] == ["generate-sample-data", "load-lending", "migrate"]
    assert len(names) == len(set(names))


def test_generate_step_is_a_no_op_when_all_files_exist(tmp_path: Path) -> None:
    from data.generator import TABLES

    for table in TABLES:
        (tmp_path / f"{table.name}.parquet").write_bytes(b"x")
    assert init.generate_sample_data(context(tmp_path)).startswith("unchanged")
    assert {p.name for p in tmp_path.iterdir()} == {f"{t.name}.parquet" for t in TABLES}
    assert all(p.read_bytes() == b"x" for p in tmp_path.iterdir())


def test_generate_step_creates_missing_files_then_is_unchanged(tmp_path: Path) -> None:
    first = init.generate_sample_data(context(tmp_path))
    assert first.startswith("generated")
    assert len(list(tmp_path.glob("*.parquet"))) == 9
    assert init.generate_sample_data(context(tmp_path)).startswith("unchanged")


def test_main_without_urls_exits_2_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    monkeypatch.delenv("MAKNA_LENDING_LOADER_URL", raising=False)
    assert init.main([]) == 2
    assert "MAKNA_DATABASE_URL" in capsys.readouterr().err
