"""WP-15C acceptance 6 (C-10, D-32 ruling 4), through the service: a request that reaches a
deprecated entity compiles and its response carries a warning with the reason and the replacement.

The entity is deprecated in a copy of the sample definitions, which are indexed again.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import Engine

from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer
from makna.model.governance import STRICT
from makna.search.embedding import HashProvider
from makna.services.derive import make_derive
from makna.services.examples import validate_for_build

from ..catalog_support import DEFINITIONS
from . import service_support as sup
from .service_support import Users


def deprecate_product(tmp_path: Path) -> Path:
    root = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, root)
    path = root / "lending" / "entities" / "product.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("status: approved", "status: deprecated", 1).replace(
            "version: 1\n",
            "version: 1\ndeprecation:\n"
            "  reason: Products are now kept in the product catalog.\n"
            "  replaced_by: entity:branch\n",
            1,
        ),
        encoding="utf-8",
    )
    return root


def test_a_request_that_joins_a_deprecated_entity_compiles_with_a_warning(
    fresh_service_world: tuple[Engine, Users], tmp_path: Path
) -> None:
    engine, users = fresh_service_world
    root = deprecate_product(tmp_path)
    assert validate_for_build(load_definitions(root)) == []  # nothing approved is built on it
    objects = definition_objects(load_definitions(root))
    indexer.build(engine, HashProvider(), objects, derive=make_derive(STRICT))
    services = sup.services(engine)
    caller = users.caller("viewer")

    joined = services.compile_query(
        caller, {"metric": "disbursement_amount", "dimensions": ["product.product_category"]}
    )
    assert joined.sql and len(joined.warnings) == 1
    warning = joined.warnings[0]
    assert "(product)" in warning and "deprecated" in warning
    assert "Products are now kept in the product catalog" in warning  # the reason
    assert "entity:branch" in warning  # the replacement

    untouched = services.compile_query(caller, {"metric": "disbursement_amount"})
    assert untouched.warnings == ()
