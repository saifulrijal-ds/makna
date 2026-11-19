"""WP-21 (CON-2): the OpenMetadata connector through the sync, against PostgreSQL.

The server is the fake transport of the unit tests, serving the committed fixtures.
"""

from __future__ import annotations

import pytest
from sqlalchemy import URL, Engine, func, select

from makna.catalog.base import ConnectorError
from makna.catalog.file import FileConnector
from makna.catalog.openmetadata import GLOSSARY_TERMS_PATH
from makna.catalog.sync import sync_catalog
from makna.cli.main import main
from makna.index.tables import catalog_dataset, catalog_source

from ..catalog_support import DEFINITIONS, EXPORT, TAG_MAP
from ..unit.test_catalog_openmetadata import SERVICES, chain, connector, sample_server, single


def om_sync(engine: Engine) -> dict[str, dict[str, int]]:
    server = sample_server()
    result = sync_catalog(engine, connector(server, services=SERVICES), tag_map=TAG_MAP)
    return result.summary.as_dict()


def test_the_sample_syncs_from_openmetadata_and_a_second_sync_changes_nothing(
    migrated_engine: Engine,
) -> None:
    first = om_sync(migrated_engine)
    assert first["datasets"]["added"] == 9 and first["glossary_terms"]["added"] == 8
    second = om_sync(migrated_engine)
    assert second["datasets"]["added"] == 0 and second["datasets"]["changed"] == 0
    assert second["datasets"]["unchanged"] == 9
    with migrated_engine.connect() as connection:
        rows = connection.execute(select(catalog_source.c.name, catalog_source.c.connector)).all()
        stored = connection.execute(select(catalog_dataset.c.catalog_id)).scalars().all()
    assert [tuple(r) for r in rows] == [("openmetadata", "openmetadata")]
    assert set(stored) == {t["id"] for b in chain("tables").values() for t in b["data"]}


def test_a_database_synced_from_the_file_refuses_openmetadata_and_keeps_its_tables(
    migrated_engine: Engine,
) -> None:
    sync_catalog(migrated_engine, FileConnector(EXPORT), tag_map=TAG_MAP)
    with pytest.raises(ConnectorError, match="already belongs to source 'lending-sample'"):
        om_sync(migrated_engine)
    with migrated_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(catalog_dataset)).scalar() == 9
        names = connection.execute(select(catalog_source.c.name)).scalars().all()
    assert "lending-sample" in names


def test_the_cli_syncs_from_openmetadata_and_reports_skipped_services(
    migrated_engine: Engine,
    migrated_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = single("hand_tables_unmapped_service.json")
    server.routes[GLOSSARY_TERMS_PATH] = {None: {"data": [], "paging": {"total": 0}}}
    monkeypatch.setenv("MAKNA_DATABASE_URL", migrated_url.render_as_string(hide_password=False))
    monkeypatch.setattr("makna.cli.main._openmetadata_connector", lambda: connector(server))
    code = main(["catalog", "sync", "--source", "openmetadata", "--definitions", str(DEFINITIONS)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "datasets: added 1" in captured.out
    assert "skipped 2 table(s) of OpenMetadata service 'marketing_sample'" in captured.err


def test_a_database_synced_from_openmetadata_refuses_the_file_and_keeps_its_tables(
    migrated_engine: Engine,
) -> None:
    om_sync(migrated_engine)
    with pytest.raises(ConnectorError, match="already belongs to source 'openmetadata'"):
        sync_catalog(migrated_engine, FileConnector(EXPORT), tag_map=TAG_MAP)
    with migrated_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(catalog_dataset)).scalar() == 9
        names = connection.execute(select(catalog_source.c.name)).scalars().all()
    assert names == ["openmetadata"]
