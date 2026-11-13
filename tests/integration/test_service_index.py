"""What the indexer stores for the service layer (ARCHITECTURE 5.4, 6.4): the available
dimensions and the policy fingerprint, the effective level of each entity dimension, the edges,
and that all of it is written only when it changes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Engine, text

from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer
from makna.model.governance import GovernancePolicy
from makna.search.embedding import HashProvider
from makna.services.derive import make_derive

from ..catalog_support import DEFINITIONS
from . import service_support as sup
from .service_support import Users


def state(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        return {k: v for k, v in connection.execute(text("SELECT key, value FROM index_state"))}


def derived(engine: Engine, object_id: str) -> Mapping[str, Any]:
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT derived FROM semantic_object WHERE id = :id"), {"id": object_id}
        ).scalar_one()
    assert isinstance(found, dict)
    return found


def test_each_approved_metric_stores_its_available_dimensions(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    found = derived(engine, "metric:npf_ratio")
    dimensions = {d["ref"]: d for d in found["dimensions"]}
    region = dimensions["branch.region_name"]
    assert region["via"] == "servicing branch" and region["type"] == "categorical"
    assert region["sensitivity"] == "internal" and region["masking"] is None
    assert region["available"] is True and region["reason"] is None
    birth = dimensions["customer.birth_year"]
    assert birth["sensitivity"] == "personal"
    assert birth["masking"] == {"method": "truncate_date", "grain": "year"}
    assert birth["available"] is True
    name = dimensions["customer.full_name"]
    assert name["available"] is False and "without a masking rule" in name["reason"]
    assert found["inputs"] == ["metric:npf_outstanding_principal", "metric:outstanding_principal"]
    assert "formula" in found


def test_a_deprecated_metric_has_dimensions_and_a_draft_has_none(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    assert "dimensions" in derived(engine, "metric:npl_ratio")
    assert "dimensions" not in derived(engine, "metric:write_off_amount")


def test_excluded_entities_are_stored_with_the_reason(service_world: tuple[Engine, Users]) -> None:
    engine, _ = service_world
    found = derived(engine, "metric:collection_rate")
    kinds = {e["entity"]: e["kind"] for e in found["excluded"]}
    assert kinds["contract"] == "inconsistent" and kinds["payment"] == "no_path"
    assert all({"entity", "kind", "reason"} == set(e) for e in found["excluded"])
    # the reason is what a curator reads: it names the relationships that differ
    reason = next(e["reason"] for e in found["excluded"] if e["entity"] == "contract")
    assert "payment.contract" in reason and "installment.contract" in reason


def test_the_entity_stores_the_effective_level_of_each_dimension(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    levels = derived(engine, "entity:customer")["dimension_sensitivity"]
    assert levels["birth_year"] == "personal" and levels["city"] == "internal"
    assert set(levels) == {
        "customer_segment",
        "occupation_group",
        "province",
        "registered_date",
        "birth_year",
        "gender",
        "income_band",
        "city",
        "full_name",
        "national_id",
        "phone_number",
        "email",
    }


def test_the_index_state_records_the_policy_and_what_the_facts_were_computed_against(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    found = state(engine)
    assert found["governance_fingerprint"] == GovernancePolicy.strict().fingerprint()
    assert found["derived_definitions_version"] == found["definitions_version"]
    assert found["derived_catalog_marker"] == "1"


def test_the_edges_are_stored(service_world: tuple[Engine, Users]) -> None:
    engine, _ = service_world
    with engine.connect() as connection:
        edges = {
            tuple(r)
            for r in connection.execute(text("SELECT src_id, dst_id, type FROM object_edge"))
        }
    assert ("metric:npf_outstanding_principal", "metric:npf_ratio", "input_of") in edges
    assert (
        "metric:npf_outstanding_principal",
        "rule:contract_snapshot.non_performing",
        "uses_rule",
    ) in edges
    assert ("glossary_term:npf", "metric:npf_ratio", "explains") in edges
    assert ("metric:npl_ratio", "metric:npf_ratio", "replaced_by") in edges
    assert ("metric:outstanding_principal", "entity:contract_snapshot", "measured_on") in edges
    assert {t for _, _, t in edges} == set(indexer.EDGE_TYPES)


def test_a_second_build_with_the_hook_writes_nothing(
    fresh_service_world: tuple[Engine, Users],
) -> None:
    engine, _ = fresh_service_world
    result = sup.index(engine)
    assert result.total_changes == 0
    assert (result.edges_added, result.edges_removed, result.issues_opened) == (0, 0, 0)
    with engine.connect() as connection:
        before = connection.execute(
            text(
                "SELECT n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables "
                "WHERE relname = 'object_edge'"
            )
        ).one()
    sup.index(engine)
    with engine.connect() as connection:
        after = connection.execute(
            text(
                "SELECT n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables "
                "WHERE relname = 'object_edge'"
            )
        ).one()
    assert tuple(before) == tuple(after)


def test_a_build_under_another_policy_changes_the_stored_dimensions_and_the_fingerprint(
    fresh_service_world: tuple[Engine, Users],
) -> None:
    engine, _ = fresh_service_world
    relaxed = GovernancePolicy.custom(allow_unmasked_personal=True)
    objects = definition_objects(load_definitions(DEFINITIONS))
    result = indexer.build(engine, HashProvider(), objects, derive=make_derive(relaxed))
    assert result.objects_changed > 0 and result.state_changed >= 1
    assert state(engine)["governance_fingerprint"] == relaxed.fingerprint()
    name = {d["ref"]: d for d in derived(engine, "metric:npf_ratio")["dimensions"]}[
        "customer.full_name"
    ]
    assert name["available"] is True


def test_a_build_without_the_hook_drops_the_stored_dimensions_and_the_service_computes_them(
    fresh_service_world: tuple[Engine, Users],
) -> None:
    from makna.services import Services

    engine, users = fresh_service_world
    caller = users.caller("viewer")
    svc = Services(engine, HashProvider(), policy=GovernancePolicy.strict())
    before = svc.get_definition(caller, "metric:npf_ratio").to_dict()
    objects = definition_objects(load_definitions(DEFINITIONS))
    indexer.build(engine, HashProvider(), objects)
    assert "dimensions" not in derived(engine, "metric:npf_ratio")
    after = Services(engine, HashProvider(), policy=GovernancePolicy.strict()).get_definition(
        caller, "metric:npf_ratio"
    )
    assert after.to_dict() == before


def test_index_object_keeps_the_stored_facts_of_other_objects_and_marks_them_stale(
    fresh_service_world: tuple[Engine, Users],
) -> None:
    engine, users = fresh_service_world
    objects = definition_objects(load_definitions(DEFINITIONS))
    target = next(o for o in objects if o.id == "metric:npf_ratio")
    stored = derived(engine, "metric:npf_ratio")["dimensions"]
    assert indexer.index_object(engine, HashProvider(), target, objects).total_changes == 0
    assert derived(engine, "metric:npf_ratio")["dimensions"] == stored  # kept, not wiped
    found = state(engine)
    assert found["derived_definitions_version"] == found["definitions_version"]
