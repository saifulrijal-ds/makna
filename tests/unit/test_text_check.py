"""Fix round 1 (safety I-1): text a person does not type is refused at the boundary."""

from __future__ import annotations

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
