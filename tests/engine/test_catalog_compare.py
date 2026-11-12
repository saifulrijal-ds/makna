"""WP-11 acceptance 5, first half: the catalog and the sample database agree.

The catalog side is the committed export, which is what `makna catalog sync` copies into the
catalog tables; the CLI itself is run against the synced copy by the CI stack job and by the
integration test. The second half (a dropped column) needs write access to a scratch database
and is in `tests/integration/test_engine_compare.py`.
"""

from __future__ import annotations

from makna.engine.compare import catalog_schemas, compare
from makna.engine.postgres import PostgresConnector
from tests.catalog_support import export_data


def test_there_are_no_differences_between_the_catalog_export_and_the_sample_database(
    engine_url: str,
) -> None:
    catalog = {
        d["engine_fqn"]: [c["name"] for c in d["columns"]] for d in export_data()["datasets"]
    }
    assert len(catalog) == 9
    connector = PostgresConnector(engine_url)
    tables = [t for schema in catalog_schemas(catalog) for t in connector.list_tables(schema)]
    assert compare(catalog, tables) == []


def test_the_comparison_notices_a_column_the_catalog_has_and_the_engine_does_not(
    engine_url: str,
) -> None:
    """The same data with one name changed on the catalog side: the sample database is not
    written to."""
    catalog = {
        d["engine_fqn"]: [c["name"] for c in d["columns"]] for d in export_data()["datasets"]
    }
    catalog["lending.core.customers"].append("loyalty_points")
    tables = PostgresConnector(engine_url).list_tables("core")
    found = compare(catalog, tables)
    assert [(d.kind, d.name) for d in found] == [
        ("missing_column", "core.customers.loyalty_points")
    ]
