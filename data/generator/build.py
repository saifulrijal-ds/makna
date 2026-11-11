"""Orchestration: plan the sizes, build every table, return Arrow tables."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from . import reference as ref
from .entities import Columns, build_applications, build_branches, build_customers, build_products
from .lifecycle import build_lifecycle
from .spec import TABLES, TableSpec
from .writer import to_arrow

DEFAULT_SEED = 20261111
BASE_CUSTOMERS = 6_000
BASE_APPLICATIONS = 9_000

_STAGES = ("branches", "customers", "applications", "lifecycle")


@dataclass(frozen=True)
class Plan:
    """Row counts fixed before generation. Child tables follow from the lifecycle."""

    branches: int
    products: int
    customers: int
    applications: int


def plan(scale: float) -> Plan:
    """Planned sizes of the tables whose size is chosen up front (section 2 targets x scale).

    Branches and products are reference data and do not scale.
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    branches = sum(c.branches for a in ref.AREAS for c in a.cities)
    customers = max(round(BASE_CUSTOMERS * scale), 1)
    applications = max(round(BASE_APPLICATIONS * scale), customers)
    return Plan(branches, len(ref.PRODUCTS), customers, applications)


def _stage_rng(seed: int, stage: str) -> np.random.Generator:
    return np.random.default_rng([seed, _STAGES.index(stage)])


def _scale_money(spec: TableSpec, columns: Columns) -> Columns:
    """Money is simulated in whole rupiah; decimal(18,2) columns take hundredths."""
    out: Columns = {}
    for column in spec.columns:
        values = columns[column.name]
        if column.sql_type == "decimal(18,2)":
            values = [v * 100 if isinstance(v, int) else v for v in values]
        out[column.name] = values
    return out


def generate(seed: int = DEFAULT_SEED, scale: float = 1.0) -> dict[str, pa.Table]:
    """Generate all nine tables. The same (seed, scale) always gives the same tables."""
    if seed < 0:
        raise ValueError("seed must not be negative")
    sizes = plan(scale)
    branches = build_branches(_stage_rng(seed, "branches"))
    products = build_products()
    customers = build_customers(_stage_rng(seed, "customers"), sizes.customers, branches)
    apps = build_applications(
        _stage_rng(seed, "applications"), sizes.applications, customers, branches, products
    )
    life = build_lifecycle(_stage_rng(seed, "lifecycle"), apps, branches)
    columns: dict[str, Columns] = {
        "branches": branches.columns,
        "products": products.columns,
        "customers": customers.columns,
        "loan_applications": apps.columns,
        "contracts": life.contracts,
        "installment_schedules": life.schedules,
        "payments": life.payments,
        "collection_actions": life.actions,
        "contract_monthly_snapshots": life.snapshots,
    }
    return {spec.name: to_arrow(spec, _scale_money(spec, columns[spec.name])) for spec in TABLES}
