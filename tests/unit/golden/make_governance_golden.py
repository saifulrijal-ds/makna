"""Write the governance golden file: the compiler's output for a seeded set of requests.

Run from the repository root:

    uv run python -m tests.unit.golden.make_governance_golden

The file `compiler_governance_golden.json` next to this script holds, for each request, the
PostgreSQL text the compiler produced or the error code it raised. It was made with the compiler
as it was before the governance modes (D-29, WP-10B) and pins that behaviour: compiling with no
policy argument, and with the `strict` policy, must reproduce it exactly. Never regenerate it
after a compiler change. Regenerate it only after a change to the sample definitions, by running
this script with the compiler of `main`, so that the cases follow the new definitions.

The script reads only the sample definitions under `semantic/definitions` and the product
packages. It calls the compiler with default arguments only, so it runs unchanged before and
after the policy argument exists. Requests come from a seeded `random.Random` and are drawn from
the model (sorted names), so the same definitions give the same file.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from makna.compiler.compile import compile_request
from makna.compiler.governance import GovernanceError
from makna.compiler.plan import available_dimensions
from makna.compiler.snapshot import Model
from makna.definitions.loader import load_definitions
from makna.model.definitions import Dimension
from makna.model.request import QueryError, QueryRequest
from makna.services.examples import model_from_definitions

ROOT = Path(__file__).resolve().parents[3]
DEFINITIONS = ROOT / "semantic" / "definitions"
OUTPUT = Path(__file__).with_name("compiler_governance_golden.json")
SEED = 20261112
PLAIN_CASES = 520
DERIVED_CASES = 260

# Columns the catalog could tag personal. Each is written (entity, column); an entity or column
# the definitions do not have is skipped. They stand for the ways a derived level reaches the
# compiler: an ordinary dimension (customer city), a balance metric's snapshot date, and an
# official date of a flow metric.
DERIVED_COLUMNS = (
    ("customer", "city"),
    ("contract_snapshot", "snapshot_date"),
    ("contract", "disbursement_date"),
    ("payment", "posting_date"),
)


def load_model() -> Model:
    return model_from_definitions(load_definitions(DEFINITIONS))


def derived_model(base: Model) -> Model:
    """The same definitions with a few columns tagged personal by the (simulated) catalog."""
    derived = {}
    for entity_name, column in DERIVED_COLUMNS:
        entity = base.entities.get(entity_name)
        if entity is not None:
            derived[(entity.source.table, column)] = "personal"
    return Model.build(
        list(base.entities.values()), list(base.metrics.values()), derived_sensitivity=derived
    )


def value_for(rng: random.Random, dimension: Dimension) -> Any:
    if dimension.values:
        return rng.choice(dimension.values).value
    if dimension.type == "numeric":
        return rng.choice([0, 1, 12, 36.5])
    if dimension.type == "time":
        return rng.choice(["2026-03-01", "2025-12-31"])
    if dimension.type == "boolean":
        return rng.choice([True, False])
    return rng.choice(["Jakarta", "x", "Java"])


def refs_of(model: Model) -> tuple[list[str], list[str]]:
    """Every dimension of the model, and those whose effective sensitivity is personal."""
    everything = sorted(f"{e.name}.{d.name}" for e in model.entities.values() for d in e.dimensions)
    personal = sorted(
        f"{e.name}.{d.name}"
        for e in model.entities.values()
        for d in e.dimensions
        if model.effective_sensitivity(e, d) == "personal"
    )
    return everything, personal


def random_fields(rng: random.Random, model: Model) -> dict[str, Any]:
    refs, personal = refs_of(model)
    metric = rng.choice(sorted(model.metrics))

    offered = sorted(available_dimensions(model, model.metrics[metric]).refs())

    def pick() -> str:
        roll = rng.random()
        if roll < 0.15 and personal:
            return rng.choice(personal)
        if roll < 0.18:
            return "customer.no_such_dimension"
        return rng.choice(offered if roll < 0.85 and offered else refs)

    dimensions = [pick() for _ in range(rng.choice([0, 1, 1, 2, 2, 3]))]
    if rng.random() < 0.3:
        dimensions.append("customer.birth_year")
    filters = []
    for _ in range(rng.choice([0, 0, 1, 2])):
        ref = pick()
        found = model.dimension(ref)
        if found is None:
            continue
        dimension = found[1]
        op = rng.choice(["=", "!=", "in", "is_null"] if dimension.type != "numeric" else ["=", ">"])
        value: Any = None
        if op == "in":
            value = [value_for(rng, dimension), value_for(rng, dimension)]
        elif op != "is_null":
            value = value_for(rng, dimension)
        filters.append({"dimension": ref, "op": op, "value": value})
    fields: dict[str, Any] = {"metric": metric, "dimensions": dimensions, "filters": filters}
    if rng.random() < 0.35:
        fields["grain"] = rng.choice(["month", "quarter", "year"])
    if rng.random() < 0.4:
        fields["time_start"] = rng.choice(["2025-01-01", "2026-01-01"])
        fields["time_end"] = rng.choice(["2026-07-01", "2026-12-31"])
    return fields


def outcome(model: Model, fields: dict[str, Any]) -> dict[str, Any]:
    try:
        request = QueryRequest.model_validate(fields)
        response = compile_request(model, request, "postgres")
    except QueryError as error:
        return {"error": str(error.code)}
    except GovernanceError:
        return {"error": "GovernanceError"}
    return {
        "sql": response.sql,
        "columns": [c.name for c in response.columns],
        "warnings": list(response.warnings),
        "explanation": list(response.explanation),
    }


def build() -> dict[str, Any]:
    plain = load_model()
    derived = derived_model(plain)
    rng = random.Random(SEED)
    cases: list[dict[str, Any]] = []
    for model_name, model, count in (
        ("plain", plain, PLAIN_CASES),
        ("derived", derived, DERIVED_CASES),
    ):
        for _ in range(count):
            fields = random_fields(rng, model)
            cases.append({"model": model_name, "request": fields, **outcome(model, fields)})
    return {"seed": SEED, "cases": cases}


def main() -> None:
    golden = build()
    OUTPUT.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n")
    cases = golden["cases"]
    errors = sum("error" in case for case in cases)
    print(f"wrote {OUTPUT.relative_to(ROOT)}: {len(cases)} cases, {errors} refused")


if __name__ == "__main__":
    main()
