"""WP-18 (review M-9): the accepted values of `MAKNA_ALLOW_SELF_APPROVAL`, as the other boolean
settings read them. The values that turn the two-person rule off are listed in
`deploy/.env.example` and in ARCHITECTURE section 10."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from makna.config import Settings


def settings(value: object) -> Settings:
    return Settings.model_validate(
        {"database_url": "postgresql://x", "secret_key": "k" * 40, "allow_self_approval": value}
    )


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", "y", "t", True])
def test_these_values_turn_the_two_person_rule_off(value: object) -> None:
    assert settings(value).allow_self_approval is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "n", "f", False])
def test_these_values_keep_it(value: object) -> None:
    assert settings(value).allow_self_approval is False


@pytest.mark.parametrize("value", ["enable", "2", " true", "maybe"])
def test_anything_else_stops_the_start(value: object) -> None:
    with pytest.raises(ValidationError):
        settings(value)
