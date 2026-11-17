"""WP-15C (Q4, D-32 ruling 3): a ratio compares the role of each input's last relationship.

`collection_rate` divides payments by installments, and both belong to a contract. Before, the
check compared `payment.contract` with `installment.contract` as different relationships and
offered no contract dimension. The number it gives by a contract dimension is checked here against
a hand-written query that groups each side by the same contract column.
"""

from __future__ import annotations

import duckdb
import pytest

from makna.compiler.compile import compile_request
from makna.compiler.plan import available_dimensions

from .compiler_support import lending, request, tree_of
from .duckdb_support import run_sql, run_tree, same_rows


def test_collection_rate_offers_the_dimensions_of_contract() -> None:
    result = available_dimensions(lending(), lending().metrics["collection_rate"])
    offered = set(result.refs())
    assert {"contract.contract_status", "contract.tenor_months"} <= offered
    assert not [e for e in result.excluded if e.entity == "contract"]


def test_collection_rate_still_leaves_out_installment_which_one_input_reads_itself() -> None:
    result = available_dimensions(lending(), lending().metrics["collection_rate"])
    reason = next(e.reason for e in result.excluded if e.entity == "installment")
    assert "reads installment itself" in reason


def test_a_ratio_by_a_contract_dimension_equals_each_side_grouped_by_the_same_column(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    got = run_tree(
        lending_db, tree_of(lending(), "collection_rate", dimensions=["contract.contract_status"])
    )
    want = run_sql(
        lending_db,
        """
        SELECT s.status, s.collected, d.due, CAST(s.collected AS DOUBLE) / NULLIF(d.due, 0)
        FROM (SELECT c.contract_status AS status,
                     SUM(p.principal_paid + p.interest_paid) AS collected
              FROM payments p JOIN contracts c ON c.contract_id = p.contract_id
              WHERE NOT p.is_reversed GROUP BY 1) s
        JOIN (SELECT c.contract_status AS status, SUM(i.installment_amount) AS due
              FROM installment_schedules i JOIN contracts c ON c.contract_id = i.contract_id
              WHERE i.is_current GROUP BY 1) d ON d.status = s.status
        ORDER BY 1
        """,
    )
    assert len(got) >= 2
    assert same_rows([r for r in got if r[0] in {w[0] for w in want}], want)


@pytest.mark.parametrize("dimension", ["contract.contract_status", "contract.tenor_months"])
def test_a_ratio_by_a_contract_dimension_compiles_for_postgres(dimension: str) -> None:
    response = compile_request(lending(), request("collection_rate", dimensions=[dimension]))
    assert "m_collected_amount" in response.sql and "m_due_amount" in response.sql
    assert response.columns[0].name == dimension.replace(".", "__")
