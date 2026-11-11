"""WP-05: the `lending.core` DDL is built from the WP-02 table specification."""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from data.generator import TABLES, TableSpec
from data.lending_loader import create_table_sql


@pytest.mark.parametrize("spec", TABLES, ids=lambda s: s.name)
def test_ddl_has_every_column_and_the_primary_key(spec: TableSpec) -> None:
    ddl = create_table_sql(spec).as_string()
    tree = sqlglot.parse_one(ddl, read="postgres")
    assert isinstance(tree, exp.Create)
    assert tree.this.this.name == spec.name
    assert tree.this.this.args["db"].name == "core"
    defs = [d for d in tree.this.expressions if isinstance(d, exp.ColumnDef)]
    assert [d.name for d in defs] == list(spec.column_names)
    keys = [k for k in tree.this.expressions if isinstance(k, exp.PrimaryKey)]
    assert [c.name for c in keys[0].expressions] == list(spec.primary_key)


def test_nine_tables() -> None:
    assert len(TABLES) == 9
