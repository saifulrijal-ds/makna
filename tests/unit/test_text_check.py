"""Fix round 1 (safety I-1): text a person does not type is refused at the boundary."""

from __future__ import annotations

from typing import Any

import pytest

from makna.services import ServiceError, ServiceErrorCode
from makna.services.text_check import check_deep, check_text, has_bad_character


@pytest.mark.parametrize(
    "text",
    ["a\x00b", "\x01", "\x1f", "\x7f", "\x80", "\x9f", "\ud800", "x\udfffy", "a\x0bb", "\x0c"],
)
def test_control_characters_and_surrogates_are_bad(text: str) -> None:
    assert has_bad_character(text)
    with pytest.raises(ServiceError) as raised:
        check_text("query", text)
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert raised.value.details["field"] == "query"


@pytest.mark.parametrize(
    "text", ["", "plain", "tab\there", "two\nlines\r\n", "Jawa Barat", "kota é中", " "]
)
def test_ordinary_text_and_whitespace_are_fine(text: str) -> None:
    assert not has_bad_character(text)
    check_text("query", text)


def test_a_nested_request_names_the_path() -> None:
    request = {"metric": "x", "filters": [{"dimension": "a.b", "op": "=", "value": "v\x00"}]}
    with pytest.raises(ServiceError) as raised:
        check_deep("", request)
    assert raised.value.details["field"] == "filters.0.value"
    with pytest.raises(ServiceError) as raised:
        check_deep("", {"dimensions": ["ok", "bad\ud800"]})
    assert raised.value.details["field"] == "dimensions.1"
    with pytest.raises(ServiceError) as raised:
        check_deep("", {"bad\x00key": 1})
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    check_deep("", {"metric": "x", "limit": 5, "filters": [{"value": 1.5}]})


def test_a_compile_request_has_size_bounds() -> None:
    from makna.services.text_check import check_request_bounds

    check_request_bounds(
        {
            "metric": "m",
            "dimensions": [f"a.b{i}" for i in range(20)],
            "filters": [{"dimension": "a.b", "op": "in", "value": list(range(100))}] * 20,
            "grain": "x" * 200,
        }
    )
    cases: list[tuple[dict[str, Any], str]] = [
        ({"dimensions": ["a.b"] * 21}, "dimensions"),
        ({"filters": [{"dimension": "a.b", "op": "=", "value": 1}] * 21}, "filters"),
        (
            {"filters": [{"dimension": "a.b", "op": "in", "value": list(range(101))}]},
            "filters.0.value",
        ),
        ({"metric": "x" * 201}, "metric"),
        ({"filters": [{"dimension": "a.b", "op": "=", "value": "y" * 201}]}, "filters.0.value"),
        ({"dimensions": ["z" * 201]}, "dimensions.0"),
    ]
    for request, field in cases:
        with pytest.raises(ServiceError) as raised:
            check_request_bounds(request)
        assert raised.value.code == ServiceErrorCode.INVALID_INPUT
        assert raised.value.details["field"] == field
