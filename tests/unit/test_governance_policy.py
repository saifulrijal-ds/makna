"""The governance policy type (D-29): an inconsistent policy cannot exist."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from makna.model.governance import (
    DEFAULT_MIN_GROUP_SIZE,
    RULES,
    STRICT,
    GovernancePolicy,
)


def test_the_default_is_strict_with_a_minimum_of_ten() -> None:
    assert STRICT == GovernancePolicy.strict()
    assert STRICT.mode == "strict"
    assert STRICT.min_group_size == DEFAULT_MIN_GROUP_SIZE == 10
    assert (STRICT.allow_personal_filters, STRICT.apply_masks, STRICT.allow_unmasked_personal) == (
        False,
        True,
        False,
    )
    assert STRICT.relaxed() == ()


def test_strict_takes_only_a_minimum_of_two_or_more() -> None:
    assert GovernancePolicy.strict(2).min_group_size == 2
    assert GovernancePolicy.strict(500).min_group_size == 500
    for bad in (1, 0, -1):
        with pytest.raises(ValueError, match="min_group_size"):
            GovernancePolicy.strict(bad)


def test_custom_defaults_every_rule_to_the_strict_value() -> None:
    policy = GovernancePolicy.custom()
    assert policy.mode == "custom"
    assert policy.min_group_size == 10
    assert (policy.allow_personal_filters, policy.apply_masks, policy.allow_unmasked_personal) == (
        False,
        True,
        False,
    )
    assert policy.relaxed() == ()


@pytest.mark.parametrize("bad", [1, -1, -10])
def test_custom_refuses_a_minimum_of_one_or_below_zero(bad: int) -> None:
    with pytest.raises(ValueError, match="min_group_size"):
        GovernancePolicy.custom(min_group_size=bad)


def test_custom_accepts_zero_and_two_or_more() -> None:
    assert GovernancePolicy.custom(min_group_size=0).relaxed() == (("min_group_size", 0),)
    assert GovernancePolicy.custom(min_group_size=2).relaxed() == ()


def test_off_relaxes_every_rule() -> None:
    off = GovernancePolicy.off()
    assert off.mode == "off"
    assert off.min_group_size == 0
    assert off.allow_personal_filters and off.allow_unmasked_personal and not off.apply_masks
    assert [name for name, _ in off.relaxed()] == list(RULES)
    assert dict(off.relaxed()) == {
        "min_group_size": 0,
        "allow_personal_filters": True,
        "apply_masks": False,
        "allow_unmasked_personal": True,
    }


@pytest.mark.parametrize(
    "fields",
    [
        # strict with a custom rule set
        dict(
            mode="strict",
            min_group_size=10,
            allow_personal_filters=True,
            apply_masks=True,
            allow_unmasked_personal=False,
        ),
        dict(
            mode="strict",
            min_group_size=10,
            allow_personal_filters=False,
            apply_masks=False,
            allow_unmasked_personal=False,
        ),
        dict(
            mode="strict",
            min_group_size=10,
            allow_personal_filters=False,
            apply_masks=True,
            allow_unmasked_personal=True,
        ),
        dict(
            mode="strict",
            min_group_size=0,
            allow_personal_filters=False,
            apply_masks=True,
            allow_unmasked_personal=False,
        ),
        # off with a rule left in force
        dict(
            mode="off",
            min_group_size=10,
            allow_personal_filters=True,
            apply_masks=False,
            allow_unmasked_personal=True,
        ),
        dict(
            mode="off",
            min_group_size=0,
            allow_personal_filters=False,
            apply_masks=False,
            allow_unmasked_personal=True,
        ),
        dict(
            mode="off",
            min_group_size=0,
            allow_personal_filters=True,
            apply_masks=True,
            allow_unmasked_personal=True,
        ),
        dict(
            mode="off",
            min_group_size=0,
            allow_personal_filters=True,
            apply_masks=False,
            allow_unmasked_personal=False,
        ),
        # custom with a minimum of one, or an unknown mode, or values of the wrong type
        dict(
            mode="custom",
            min_group_size=1,
            allow_personal_filters=False,
            apply_masks=True,
            allow_unmasked_personal=False,
        ),
        dict(
            mode="loose",
            min_group_size=10,
            allow_personal_filters=False,
            apply_masks=True,
            allow_unmasked_personal=False,
        ),
        dict(
            mode="custom",
            min_group_size=10.0,
            allow_personal_filters=False,
            apply_masks=True,
            allow_unmasked_personal=False,
        ),
        dict(
            mode="custom",
            min_group_size=True,
            allow_personal_filters=False,
            apply_masks=True,
            allow_unmasked_personal=False,
        ),
        dict(
            mode="custom",
            min_group_size=10,
            allow_personal_filters=1,
            apply_masks=True,
            allow_unmasked_personal=False,
        ),
    ],
)
def test_an_inconsistent_policy_cannot_be_built(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GovernancePolicy(**fields)


def test_a_copy_with_replaced_fields_is_checked_too() -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(STRICT, allow_unmasked_personal=True)
    with pytest.raises(ValueError):
        dataclasses.replace(GovernancePolicy.off(), apply_masks=True)
    assert dataclasses.replace(GovernancePolicy.custom(), apply_masks=False).relaxed() == (
        ("apply_masks", False),
    )


def test_a_policy_is_frozen_and_hashable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        STRICT.apply_masks = False  # type: ignore[misc]
    assert len({STRICT, GovernancePolicy.strict(10), GovernancePolicy.off()}) == 2


@pytest.mark.parametrize(
    ("policy", "has_mask", "raw"),
    [
        (STRICT, True, False),
        (STRICT, False, False),
        (GovernancePolicy.custom(apply_masks=False), True, True),
        (GovernancePolicy.custom(apply_masks=False), False, False),
        (GovernancePolicy.custom(allow_unmasked_personal=True), True, False),
        (GovernancePolicy.custom(allow_unmasked_personal=True), False, True),
        (GovernancePolicy.off(), True, True),
        (GovernancePolicy.off(), False, True),
    ],
)
def test_may_return_raw(policy: GovernancePolicy, has_mask: bool, raw: bool) -> None:
    assert policy.may_return_raw(has_mask) is raw
