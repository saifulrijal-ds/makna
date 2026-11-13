"""WP-12 pure parts: the effective level of a dimension, the edges the indexer states, the values
a search document may carry, the policy fingerprint, `GovernanceError` naming its column, the
application's services, and the governance settings the indexer's hook needs."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from makna.app import create_app
from makna.compiler.compile import compile_request
from makna.compiler.governance import GovernanceError
from makna.compiler.snapshot import Model
from makna.config import ConfigError, load_governance_runtime, load_settings
from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer
from makna.model.definitions import Dimension, Entity
from makna.model.governance import STRICT, GovernancePolicy
from makna.model.request import QueryRequest
from makna.model.sensitivity import columns_read, effective_level
from makna.services import ServiceError, Services

from .compiler_support import DEFINITIONS, lending

ENV = {"MAKNA_DATABASE_URL": "postgresql://u:p@localhost/makna", "MAKNA_SECRET_KEY": "k"}


def sample_objects() -> list:  # type: ignore[type-arg]
    return definition_objects(load_definitions(DEFINITIONS))


def entity_named(name: str) -> Entity:
    found = lending().entities[name]
    return found


def dimension_named(entity: Entity, name: str) -> Dimension:
    return next(d for d in entity.dimensions if d.name == name)


# --- effective level ---


def test_effective_level_is_the_stricter_of_declared_and_derived() -> None:
    customer = entity_named("customer")
    city = dimension_named(customer, "city")
    assert effective_level(customer, city, {}) == "internal"
    derived = {("lending.core.customers", "city"): "personal"}
    assert effective_level(customer, city, derived) == "personal"
    # A derived level can only tighten.
    loose = {("lending.core.customers", "city"): "public"}
    assert effective_level(customer, city, loose) == "internal"
    # The key is lower case, as the catalog stores it, and the table is the entity's source.
    assert effective_level(customer, city, {("lending.core.customers", "City"): "personal"}) == (
        "internal"
    )


def test_the_model_and_the_function_agree_for_every_dimension() -> None:
    derived = {
        ("lending.core.customers", "city"): "personal",
        ("lending.core.branches", "province"): "confidential",
    }
    model = Model.build(list(lending().entities.values()), [], derived_sensitivity=derived)
    for entity in model.entities.values():
        for dimension in entity.dimensions:
            assert model.effective_sensitivity(entity, dimension) == effective_level(
                entity, dimension, derived
            )


def test_columns_read_reads_the_expression_or_the_name() -> None:
    customer = entity_named("customer")
    assert columns_read(dimension_named(customer, "city")) == ("city",)
    assert columns_read(dimension_named(customer, "registered_date")) == ("registered_at",)


# --- value text ---


def test_the_values_of_a_personal_dimension_are_never_indexed() -> None:
    customer = entity_named("customer")
    gender = dimension_named(customer, "gender")
    assert "Female" in indexer._value_text(gender)
    assert indexer._value_text(gender, "personal") == ""
    lookup = indexer._Lookup(sample_objects(), {("lending.core.customers", "gender"): "personal"})
    assert lookup.level(customer, gender) == "personal"
    plain = indexer._Lookup(sample_objects())
    assert plain.level(customer, gender) == "internal"


def test_a_catalog_tag_removes_the_values_from_the_search_document() -> None:
    objects = sample_objects()
    target = next(o for o in objects if o.id == "entity:customer")

    def extra_of(derived: dict[tuple[str, str], str]) -> str:
        documents = indexer.object_documents(target, indexer._Lookup(objects, derived))
        return next(d for d in documents if d.object_id == "dimension:customer.gender").extra

    assert "Female" in extra_of({})
    assert extra_of({("lending.core.customers", "gender"): "personal"}) == ""


def test_the_entity_row_records_the_level_of_each_dimension() -> None:
    objects = sample_objects()
    target = next(o for o in objects if o.id == "entity:customer")
    row = indexer.semantic_row(
        target, indexer._Lookup(objects, {("lending.core.customers", "city"): "personal"})
    )
    levels = row["derived"]["dimension_sensitivity"]
    assert levels["city"] == "personal" and levels["birth_year"] == "personal"
    assert levels["gender"] == "internal"


# --- edges ---


def all_edges() -> set[indexer.Edge]:
    objects = sample_objects()
    lookup = indexer._Lookup(objects)
    found: set[indexer.Edge] = set()
    for obj in objects:
        found |= indexer.object_edges(obj, lookup)
    return found


def test_a_ratio_metric_states_its_inputs_and_a_simple_one_its_entity_and_rules() -> None:
    edges = all_edges()
    assert ("metric:npf_outstanding_principal", "metric:npf_ratio", "input_of") in edges
    assert ("metric:outstanding_principal", "metric:npf_ratio", "input_of") in edges
    assert ("metric:outstanding_principal", "entity:contract_snapshot", "measured_on") in edges
    assert (
        "metric:outstanding_principal",
        "rule:contract_snapshot.in_book",
        "uses_rule",
    ) in edges
    assert (
        "metric:npf_outstanding_principal",
        "rule:contract_snapshot.non_performing",
        "uses_rule",
    ) in edges


def test_terms_explain_what_they_relate_to_and_a_deprecated_object_names_its_replacement() -> None:
    edges = all_edges()
    assert ("glossary_term:npf", "metric:npf_ratio", "explains") in edges
    assert ("metric:npl_ratio", "metric:npf_ratio", "replaced_by") in edges
    assert any(kind == "joins_to" for _, _, kind in edges)
    assert {kind for _, _, kind in edges} <= set(indexer.EDGE_TYPES)


def test_a_ratio_metric_states_no_rule_of_its_own() -> None:
    assert not [e for e in all_edges() if e[0] == "metric:npf_ratio" and e[2] == "uses_rule"]


# --- policy fingerprint ---


def test_the_fingerprint_changes_with_every_rule_and_is_stable() -> None:
    policies = [
        STRICT,
        GovernancePolicy.strict(3),
        GovernancePolicy.custom(),
        GovernancePolicy.custom(min_group_size=0),
        GovernancePolicy.custom(allow_personal_filters=True),
        GovernancePolicy.custom(apply_masks=False),
        GovernancePolicy.custom(allow_unmasked_personal=True),
        GovernancePolicy.off(),
    ]
    prints = [p.fingerprint() for p in policies]
    assert GovernancePolicy.strict().fingerprint() == STRICT.fingerprint()
    # `strict` and `custom()` differ only in the mode name, and the fingerprint tells them apart.
    assert STRICT.fingerprint() != GovernancePolicy.custom().fingerprint()
    assert len(set(prints)) == len(prints)


def test_the_fingerprint_follows_the_values_not_the_object() -> None:
    changed = dataclasses.replace(GovernancePolicy.custom(), allow_personal_filters=True)
    assert (
        changed.fingerprint() == GovernancePolicy.custom(allow_personal_filters=True).fingerprint()
    )


# --- GovernanceError names the column ---


def test_a_personal_join_key_ends_in_a_governance_error_that_names_table_and_column() -> None:
    model = Model.build(
        list(lending().entities.values()),
        list(lending().metrics.values()),
        derived_sensitivity={("lending.core.contracts", "customer_id"): "personal"},
    )
    request = QueryRequest(metric="active_contract_count", dimensions=("customer.city",))
    with pytest.raises(GovernanceError) as raised:
        compile_request(model, request)
    assert (raised.value.table, raised.value.column) == ("lending.core.contracts", "customer_id")
    assert "lending.core.contracts.customer_id" in str(raised.value)


def test_a_governance_error_without_names_has_none() -> None:
    error = GovernanceError("internal error: result columns would share the name x")
    assert error.table is None and error.column is None


# --- the application and the settings ---


def test_create_app_builds_the_services_and_healthz_needs_no_database() -> None:
    app = create_app(load_settings(ENV, config_path="no-such-file.toml"))
    services = app.state.services
    assert isinstance(services, Services)
    assert services.policy == STRICT
    response = TestClient(app).get("/healthz")  # no database runs at localhost: still ok
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    # No route beyond /healthz: the MCP server (WP-13) and the web pages (WP-16) add theirs.
    assert {getattr(route, "path", None) for route in app.routes} == {"/healthz"}


def test_create_app_builds_the_services_with_the_configured_policy(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text('[governance]\nmode = "custom"\nallow_personal_filters = true\n')
    app = create_app(load_settings(ENV, config_path=config))
    assert app.state.services.policy == GovernancePolicy.custom(allow_personal_filters=True)


def test_create_app_fails_at_start_for_a_dialect_it_cannot_compile(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text('dialect = "oracle"\n')
    with pytest.raises(ConfigError, match="dialect"):
        create_app(load_settings(ENV, config_path=config))


def test_the_governance_runtime_needs_no_secret(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text('[governance]\nmode = "off"\n')
    runtime = load_governance_runtime({}, config_path=config)
    assert runtime.governance.policy() == GovernancePolicy.off()
    assert runtime.dialect == "postgres"
    assert (
        load_governance_runtime({}, config_path="no-such-file.toml").governance.policy() == STRICT
    )


def test_the_service_error_class_is_exported() -> None:
    assert issubclass(ServiceError, Exception)
