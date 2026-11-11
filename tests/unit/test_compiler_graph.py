"""WP-08 criterion 1: the join path rule of ARCHITECTURE section 7.3."""

from __future__ import annotations

import pytest

from makna.compiler.graph import find_paths, path_names, resolve_path
from makna.model.request import ErrorCode, QueryError

from .compiler_support import add_relationship, entity_doc, lending, shop
from .definition_fixtures import Files


def test_contract_to_customer_is_the_one_step_path() -> None:
    path = resolve_path(lending(), "contract", "customer")
    assert path_names(path) == ["contract.customer"]
    assert path[0].to == "customer"


def test_the_direct_path_beats_the_two_step_path_through_application() -> None:
    paths = find_paths(lending(), "contract", "customer")
    assert [path_names(p) for p in paths] == [["contract.customer"]]


def test_contract_to_branch_without_a_pin_is_ambiguous_and_lists_both_paths() -> None:
    with pytest.raises(QueryError) as info:
        resolve_path(lending(), "contract", "branch")
    assert info.value.code == ErrorCode.AMBIGUOUS_JOIN_PATH
    assert info.value.details["candidate_paths"] == [
        ["contract.booking_branch"],
        ["contract.servicing_branch"],
    ]
    assert "join_paths" in info.value.message


def test_a_pin_returns_the_pinned_path() -> None:
    pinned = {"branch": ["contract.servicing_branch"]}
    path = resolve_path(lending(), "contract", "branch", pinned)
    assert path_names(path) == ["contract.servicing_branch"]


def test_a_pin_may_be_longer_than_the_shortest_path() -> None:
    pinned = {"branch": ["contract.customer", "customer.home_branch"]}
    path = resolve_path(lending(), "contract", "branch", pinned)
    assert path_names(path) == ["contract.customer", "customer.home_branch"]


def test_branch_to_contract_has_no_path() -> None:
    with pytest.raises(QueryError) as info:
        resolve_path(lending(), "branch", "contract")
    assert info.value.code == ErrorCode.NO_JOIN_PATH
    assert info.value.details["from"] == "branch" and info.value.details["to"] == "contract"


def test_relationships_are_never_walked_backwards() -> None:
    assert find_paths(lending(), "customer", "contract") == ()


def test_an_entity_reaches_itself_with_the_empty_path() -> None:
    assert resolve_path(lending(), "contract", "contract") == ()


def test_the_path_through_a_second_entity_is_found() -> None:
    path = resolve_path(lending(), "payment", "customer")
    assert path_names(path) == ["payment.contract", "contract.customer"]


@pytest.mark.parametrize(
    "pinned",
    [
        {"branch": ["contract.customer"]},  # ends at customer
        {"branch": ["payment.contract", "contract.servicing_branch"]},  # starts elsewhere
        {"branch": ["contract.no_such_link"]},
    ],
)
def test_a_pin_that_is_not_a_chain_to_the_target_is_refused(pinned: dict[str, list[str]]) -> None:
    with pytest.raises(QueryError) as info:
        resolve_path(lending(), "contract", "branch", pinned)
    assert info.value.code == ErrorCode.NO_JOIN_PATH
    assert info.value.details["pinned"] == pinned["branch"]


def test_paths_longer_than_four_steps_are_not_considered() -> None:
    # order > customer > region is two steps; add four more steps behind region.
    def mutate(files: Files) -> None:
        names = ["order", "a1", "a2", "a3", "a4", "a5"]
        for name in names[1:]:
            files[f"shop/entities/{name}.yaml"] = {
                **entity_doc(files, "region"),
                "name": name,
                "relationships": [],
            }
        for source, target in zip(names, names[1:], strict=False):
            add_relationship(files, source, f"to_{target}", target)

    model = shop(mutate)
    assert [path_names(p) for p in find_paths(model, "order", "a4")] == [
        ["order.to_a1", "a1.to_a2", "a2.to_a3", "a3.to_a4"]
    ]
    assert find_paths(model, "order", "a5") == ()


def test_the_shortest_of_several_paths_wins_even_if_longer_ones_exist() -> None:
    # order reaches region directly (1 step) and through customer (2 steps).
    model = shop(lambda files: add_relationship(files, "order", "billing_region", "region"))
    assert [path_names(p) for p in find_paths(model, "order", "region")] == [
        ["order.billing_region"]
    ]


def test_a_path_visits_no_entity_twice() -> None:
    # customer > region > customer would be a cycle; the search must end, with one path each.
    model = shop(lambda files: add_relationship(files, "region", "back", "customer"))
    assert [path_names(p) for p in find_paths(model, "order", "region")] == [
        ["order.customer", "customer.home_region"]
    ]
    assert [path_names(p) for p in find_paths(model, "order", "customer")] == [["order.customer"]]
