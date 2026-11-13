"""WP-12 criterion 3 (`get_definition` returns the fields of section 12), hidden objects, and
personal values (ARCHITECTURE section 7.6, safety review M-5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from makna.model.governance import GovernancePolicy
from makna.services import ServiceError
from makna.services.errors import ServiceErrorCode

from ..catalog_support import export_with_tags
from . import service_support as sup
from .service_support import Users


def definition(world: tuple[Engine, Users], role: str, object_id: str, **kw: Any) -> dict[str, Any]:
    engine, users = world
    return sup.services(engine).get_definition(users.caller(role), object_id, **kw).to_dict()


def by_ref(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["ref"]: item for item in items}


def test_a_metric_has_every_field_of_the_section_12_example(
    service_world: tuple[Engine, Users],
) -> None:
    found = definition(service_world, "viewer", "metric:npf_ratio")
    assert found["id"] == "metric:npf_ratio" and found["label"] == "NPF Ratio"
    assert found["status"] == "approved" and found["version"] == 1
    assert found["description"].startswith("Share of outstanding principal")
    assert found["formula"] == "NPF Outstanding Principal divided by Outstanding Principal"
    assert found["type"] == "ratio" and found["unit"] == "percent"
    assert found["inputs"] == ["metric:npf_outstanding_principal", "metric:outstanding_principal"]
    assert found["owner"] == "credit-risk" and found["sensitivity"] == "internal"
    assert found["time"] == {
        "dimension": "contract_snapshot.snapshot_date",
        "grains": ["month", "quarter", "year"],
        "default_grain": "month",
        "note": "Balance metric: a period longer than a month reports its last month end.",
    }
    assert [r["ref"] for r in found["rules"]] == [
        "contract_snapshot.in_book",
        "contract_snapshot.non_performing",
    ]
    assert found["rules"][1]["description"].startswith("A position more than 90 days past due")
    assert found["example_requests"] == [
        {
            "question": "Rasio NPF per wilayah setiap kuartal dari Q4 2025 sampai Q2 2026",
            "request": {
                "dimensions": ["branch.region_name"],
                "grain": "quarter",
                "time_start": "2025-10-01",
                "time_end": "2026-07-01",
            },
        }
    ]
    assert found["caveats"].startswith("Gross ratio based on principal")
    assert "What is our NPF?" in found["example_questions"]
    assert "issues" not in found and "warnings" not in found  # nothing to report: left out


def test_dimensions_carry_path_label_type_values_and_masking(
    service_world: tuple[Engine, Users],
) -> None:
    found = definition(service_world, "viewer", "metric:npf_ratio")
    dimensions = by_ref(found["dimensions"])
    region = dimensions["branch.region_name"]
    assert region["type"] == "categorical" and region["via"] == "servicing branch"
    assert region["values"] == [
        "Jawa Barat",
        "Jawa Tengah",
        "Jawa Timur",
        "Sumatera",
        "Kalimantan Sulawesi",
    ]
    assert region["description"].startswith("Sales region")
    birth = dimensions["customer.birth_year"]
    assert birth["type"] == "time" and birth["masked"] == "year only"
    assert birth["filterable"] is False and birth["sensitivity"] == "personal"
    assert birth["note"] == "groups built from fewer than 10 customer records are left out"
    assert "values" not in birth
    assert "filterable" not in region  # true is the default and is left out
    assert dimensions["contract.contract_status"]["via"] == "contract"


def test_personal_dimensions_without_a_mask_are_listed_as_unavailable_with_a_reason(
    service_world: tuple[Engine, Users],
) -> None:
    found = definition(service_world, "viewer", "metric:npf_ratio")
    assert by_ref(found["unavailable_dimensions"]) == {
        ref: {"ref": ref, "reason": "personal data"}
        for ref in (
            "customer.full_name",
            "customer.national_id",
            "customer.phone_number",
            "customer.email",
        )
    }
    assert not {d["ref"] for d in found["dimensions"]} & set(
        by_ref(found["unavailable_dimensions"])
    )


def test_brief_leaves_out_descriptions_and_values_of_dimensions(
    service_world: tuple[Engine, Users],
) -> None:
    full = definition(service_world, "viewer", "metric:npf_ratio")
    brief = definition(service_world, "viewer", "metric:npf_ratio", detail="brief")
    assert [d["ref"] for d in brief["dimensions"]] == [d["ref"] for d in full["dimensions"]]
    for dimension in brief["dimensions"]:
        assert not {"description", "values", "value_notes"} & set(dimension)
    assert brief["description"] == full["description"]  # only the dimensions are lighter
    assert len(json.dumps(brief)) < len(json.dumps(full))


def test_a_simple_metric_and_a_collection_metric_show_their_time_dimensions(
    service_world: tuple[Engine, Users],
) -> None:
    simple = definition(service_world, "viewer", "metric:disbursement_amount")
    assert simple["time"]["dimension"] == "contract.disbursement_date"
    assert "note" not in simple["time"]  # not a balance
    assert simple["inputs"] == ["measure:contract.disbursed_principal"]
    mixed = definition(service_world, "viewer", "metric:collection_rate")
    assert sorted(mixed["time"]["dimensions"]) == ["installment.due_date", "payment.posting_date"]
    assert "dimension" not in mixed["time"]


def test_a_deprecated_metric_can_be_read_by_its_id_and_names_its_replacement(
    service_world: tuple[Engine, Users],
) -> None:
    for role in ("viewer", "curator"):
        found = definition(service_world, role, "metric:npl_ratio")
        assert found["status"] == "deprecated"
        assert found["deprecation"]["replaced_by"] == "metric:npf_ratio"
        assert found["deprecation"]["reason"]


def test_an_entity_lists_dimensions_rules_and_relationships(
    service_world: tuple[Engine, Users],
) -> None:
    found = definition(service_world, "viewer", "entity:contract_snapshot")
    assert found["kind"] == "entity" and found["domain"] == "lending"
    assert {r["ref"] for r in found["rules"]} >= {"contract_snapshot.non_performing"}
    assert {r["name"] for r in found["relationships"]} >= {"contract"}
    customer = definition(service_world, "viewer", "entity:customer")
    assert {d["ref"] for d in customer["unavailable_dimensions"]} >= {
        "customer.full_name",
        "customer.email",
    }
    assert "values" not in by_ref(customer["dimensions"])["customer.birth_year"]


def test_dimension_rule_term_domain_and_catalog_objects_return_what_section_12_says(
    service_world: tuple[Engine, Users],
) -> None:
    dimension = definition(service_world, "viewer", "dimension:branch.region_name")
    assert dimension["kind"] == "dimension" and dimension["entity"] == "branch"
    assert dimension["type"] == "categorical" and "Jawa Barat" in dimension["values"]
    birth = definition(service_world, "viewer", "dimension:customer.birth_year")
    assert birth["sensitivity"] == "personal" and "values" not in birth
    rule = definition(service_world, "viewer", "rule:contract_snapshot.non_performing")
    assert rule["kind"] == "rule" and rule["entity"] == "contract_snapshot"
    assert rule["description"].startswith("A position more than 90 days past due")
    assert "expr" not in json.dumps(rule)
    term = definition(service_world, "viewer", "glossary_term:npf")
    assert {r["id"] for r in term["related"]} == {
        "metric:npf_ratio",
        "metric:npf_outstanding_principal",
    }
    assert definition(service_world, "viewer", "domain:lending")["kind"] == "domain"
    column = definition(service_world, "viewer", "column:lending.core.customers.city")
    assert (column["table"], column["data_type"]) == ("lending.core.customers", "string")
    catalog_term = definition(service_world, "viewer", "catalog_term:glossary.npf")
    assert catalog_term["kind"] == "catalog_term" and "kredit macet" in catalog_term["synonyms"]


def test_a_personal_catalog_column_is_hidden_from_viewers_and_tokens(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    object_id = "column:lending.core.customers.national_id"
    for caller in (users.caller("viewer"), users.token()):
        with pytest.raises(ServiceError) as raised:
            svc.get_definition(caller, object_id)
        assert raised.value.code == ServiceErrorCode.NOT_FOUND
    assert svc.get_definition(users.caller("curator"), object_id).sensitivity == "personal"


# --- hidden objects stay hidden ---


def test_a_draft_is_not_found_for_a_viewer_and_a_token_exactly_like_a_missing_id(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    seen = []
    for caller in (users.caller("viewer"), users.token("admin")):
        for object_id in ("metric:write_off_amount", "metric:write_off_amounx"):
            with pytest.raises(ServiceError) as raised:
                svc.get_definition(caller, object_id)
            error = raised.value
            assert error.code == ServiceErrorCode.NOT_FOUND
            assert set(error.details) == {"id", "close_ids"}
            assert "metric:write_off_amount" not in error.details["close_ids"]
            assert "write_off" not in error.message.replace(object_id, "")
            seen.append((object_id, error.message.replace(object_id, "<id>")))
    # the draft and the id that never existed are answered in the same words
    assert seen[0][1] == seen[1][1]
    # a curator reads it
    curator = svc.get_definition(users.caller("curator"), "metric:write_off_amount")
    assert curator.status == "draft"


def test_close_ids_are_the_nearest_three_ids_the_caller_may_see(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    with pytest.raises(ServiceError) as raised:
        svc.get_definition(users.caller("viewer"), "metric:npf_ratoi")
    close = raised.value.details["close_ids"]
    assert close[0] == "metric:npf_ratio" and len(close) <= 3
    with pytest.raises(ServiceError) as raised:
        svc.get_definition(users.caller("curator"), "metric:write_off_amunt")
    assert "metric:write_off_amount" in raised.value.details["close_ids"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "npf_ratio",
        "metric",
        "metric:",
        ":npf",
        "metric:npf ratio",
        "bogus:npf_ratio",
        "x" * 201,
    ],
)
def test_a_malformed_id_is_invalid_input(service_world: tuple[Engine, Users], bad: str) -> None:
    engine, users = service_world
    with pytest.raises(ServiceError) as raised:
        sup.services(engine).get_definition(users.caller("viewer"), bad)
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert raised.value.details["field"] == "id"


def test_an_id_of_200_characters_is_looked_up_and_one_of_201_is_refused(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    with pytest.raises(ServiceError) as raised:
        svc.get_definition(caller, "metric:" + "a" * 193)
    assert raised.value.code == ServiceErrorCode.NOT_FOUND
    with pytest.raises(ServiceError) as raised:
        svc.get_definition(caller, "metric:" + "a" * 194)
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert raised.value.details == {"field": "id", "maximum": 200}


def test_detail_must_be_brief_or_full(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    with pytest.raises(ServiceError) as raised:
        sup.services(engine).get_definition(users.caller("viewer"), "metric:npf_ratio", detail="x")
    assert raised.value.details["field"] == "detail"


def test_a_draft_metric_has_no_dimensions_and_says_so_to_the_curator(
    service_world: tuple[Engine, Users],
) -> None:
    curator = definition(service_world, "curator", "metric:write_off_amount")
    assert "not approved" in curator["warnings"][0]
    assert "dimensions" not in curator  # not computed for a draft
    assert curator["time"] == {"grains": ["month"], "default_grain": "month"}


# --- personal values never leave Makna ---


@pytest.fixture
def tagged_gender(empty_database: URL, tmp_path: Path):  # type: ignore[no-untyped-def]
    """The sample world where the catalog tags customers.gender personal, though the definition
    declares it internal and lists its values (F, M)."""
    export = export_with_tags(
        tmp_path / "tagged.yaml", {("customers", "gender"): ["PII.Sensitive"]}
    )
    users = sup.build(empty_database, export=export)
    engine = create_engine(empty_database)
    yield engine, users
    engine.dispose()


def values_text(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "SELECT extra FROM search_document "
                    "WHERE object_id = 'dimension:customer.gender'"
                )
            ).scalar_one()
        )


def test_values_of_a_dimension_the_catalog_tags_personal_are_never_shown_or_indexed(
    tagged_gender: tuple[Engine, Users],
) -> None:
    engine, users = tagged_gender
    assert values_text(engine) == ""  # the indexer left the values out of the search document
    outputs = []
    for policy in (
        GovernancePolicy.strict(),
        GovernancePolicy.custom(allow_unmasked_personal=True),  # lets the dimension be selected
    ):
        svc = sup.services(engine, policy)
        for role in ("viewer", "curator"):
            caller = users.caller(role)
            outputs.append(svc.get_definition(caller, "entity:customer").to_dict())
            outputs.append(svc.get_definition(caller, "dimension:customer.gender").to_dict())
            outputs.append(svc.get_definition(caller, "metric:new_customer_count").to_dict())
            outputs.append(svc.search(caller, "gender sex", kinds=["dimension"]).to_dict())
            outputs.append(svc.list_related(caller, "entity:customer").to_dict())
    for output in outputs:
        text_form = json.dumps(output)
        assert "Female" not in text_form and "Male." not in text_form
        assert '"values": ["F"' not in text_form
    # under the relaxed policy the dimension is selectable and still has no values
    relaxed = sup.services(engine, GovernancePolicy.custom(allow_unmasked_personal=True))
    metric = relaxed.get_definition(users.caller("viewer"), "metric:new_customer_count").to_dict()
    gender = by_ref(metric["dimensions"])["customer.gender"]
    assert "values" not in gender and gender["sensitivity"] == "personal"
    assert "returned unmasked" in gender["note"]
    strict = sup.services(engine).get_definition(
        users.caller("viewer"), "metric:new_customer_count"
    )
    assert "customer.gender" in {u.ref for u in strict.unavailable_dimensions}


def test_a_declared_personal_dimension_shows_no_values_either(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    with engine.connect() as connection:
        extra = connection.execute(
            text(
                "SELECT extra FROM search_document "
                "WHERE object_id = 'dimension:customer.birth_year'"
            )
        ).scalar_one()
    assert extra == ""
    found = sup.services(engine).get_definition(
        users.caller("viewer"), "dimension:customer.birth_year"
    )
    assert found.values == []
