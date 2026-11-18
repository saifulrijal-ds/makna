"""WP-17 acceptance 6, first half: after `makna freshness refresh` on the sample data, the page of
`outstanding_principal` shows "data up to 2026-06-30".

The scratch application database (sample catalog and definitions indexed) comes from the
`eval_runtime` fixture; the engine is the sample database read through `MAKNA_ENGINE_URL`.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from evals.runtime import Runtime
from makna.cli.main import main
from makna.index.tables import data_freshness
from tests.integration import web_support as w


def test_the_metric_page_shows_the_newest_snapshot_date_after_a_refresh(
    eval_runtime: Runtime,
    engine_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = eval_runtime.engine
    monkeypatch.setenv(
        "MAKNA_DATABASE_URL", engine.url.set(drivername="postgresql").render_as_string(False)
    )
    monkeypatch.setenv("MAKNA_ENGINE_URL", engine_url)
    app = w.make_app(engine.url)
    try:
        client = w.signed_in(app, w.make_user(engine, "viewer"))
        before = client.get("/metrics/outstanding_principal").text
        assert "freshness not available" in before and "data up to" not in before

        assert main(["freshness", "refresh"]) == 0
        out = capsys.readouterr().out
        assert "failed: 0" in out and "without a date: 0" in out

        page = client.get("/metrics/outstanding_principal").text
        assert "data up to 2026-06-30" in page
        assert re.search(r"data up to 2026-06-30, checked (just now|\d+ minutes? ago)", page)
        with engine.connect() as connection:
            rows = list(connection.execute(select(data_freshness)))
        assert rows and all(r.latest_value is not None for r in rows)
        snapshot_rows = [r for r in rows if r.object_id.startswith("measure:contract_snapshot.")]
        assert snapshot_rows and {str(r.latest_value) for r in snapshot_rows} == {"2026-06-30"}
        # A second run changes the dates of nothing and adds no row.
        assert main(["freshness", "refresh"]) == 0
        with engine.connect() as connection:
            assert len(list(connection.execute(select(data_freshness)))) == len(rows)
    finally:
        app.state.services.dispose()
