"""WP-12 criterion 6 (the pure part): what `redact` removes (D-24, GOV-2)."""

from __future__ import annotations

import re

import pytest

from makna.services.redact import REDACTED, clip, redact, redact_value


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "4111.1111.1111.1111",
        "4111111111111111",
        "3201010101900001",
        "3201 0101 9000 01",
        "123456789",
        "1 2 3 4 5 6 7 8 9",
        "a 0123456789012 b",
    ],
)
def test_runs_of_nine_or_more_digits_are_removed(text: str) -> None:
    assert not re.search(r"\d", redact(text)), redact(text)


@pytest.mark.parametrize("text", ["12345678", "1234 5678", "2025-10-01", "Q4 2025", "10000", "0.5"])
def test_shorter_numbers_and_dates_stay(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "john.doe@example.com",
        "JOHN@EXAMPLE.CO.ID",
        "a+b@sub.example.org",
        "mail me: x@y",
        "john@",
        "@example.com",
    ],
)
def test_email_addresses_are_removed(text: str) -> None:
    assert "@" not in redact(text)


@pytest.mark.parametrize(
    "phone",
    [
        "+62 812-3456-7890",
        "+62 812 3456 7890",
        "+62812.3456.7890",
        "+6281234567890",
        "0812-3456-7890",
        "0812 3456 7890",
        "0812.3456.7890",
        "081234567890",
        "021-5555-1234",
        "(021) 5555 1234",
        "021 5555 1234",
        "022-555-123",
        "+62 21 555 1234",
        "+1 (415) 555-2671",
        "+44 20 7946 0958",
        "+65 6123 4567",
    ],
)
def test_phone_numbers_are_removed_whole(phone: str) -> None:
    out = redact(f"call {phone} please")
    assert out == f"call {REDACTED} please"


def test_a_query_with_an_email_a_card_number_and_a_phone_loses_all_three() -> None:
    text = (
        "NPF for john.doe@example.com card 4111 1111 1111 1111 phone +62 812-3456-7890 per wilayah"
    )
    out = redact(text)
    assert out == f"NPF for {REDACTED} card {REDACTED} phone {REDACTED} per wilayah"
    assert not re.search(r"\d{4}|@", out)


def test_redaction_is_idempotent() -> None:
    once = redact("x@y.com 4111 1111 1111 1111 +62 812 3456 7890")
    assert redact(once) == once


def test_clip_leaves_no_start_of_a_number_at_the_cut() -> None:
    text = "abc " + "1234567890123456"
    assert clip(text, 20) == text
    assert clip(text, 12) == "abc"
    assert clip("short", 100) == "short"
    assert clip("word " * 3 + "+62 812 3456", 17) == "word word word"


def test_redact_value_walks_json_like_values() -> None:
    value = {
        "q": "mail a@b.com",
        "list": ["4111 1111 1111 1111", 5, True, None],
        "n": 1234567890,
        "deep": {"x": ["+62 812 3456 7890"]},
    }
    out = redact_value(value)
    assert out == {
        "q": f"mail {REDACTED}",
        "list": [REDACTED, 5, True, None],
        "n": REDACTED,
        "deep": {"x": [REDACTED]},
    }


def test_redact_value_clips_long_text_and_marks_other_objects() -> None:
    assert len(redact_value("x" * 1000, 50)) == 50
    assert redact_value(object()) == REDACTED
