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


# --- fix round 1: normalisation, more separators, spelled-out emails, dates ----------------------

CARD = "4111111111111111"


@pytest.mark.parametrize(
    "text",
    [
        "4111/1111/1111/1111",
        "4111_1111_1111_1111",
        "4111,1111,1111,1111",
        "4111–1111–1111–1111",  # en dash
        "4111—1111—1111—1111",  # em dash
        "4111‐1111‐1111‐1111",  # hyphen
        "4111−1111−1111−1111",  # minus sign
        "4111 - 1111 - 1111 - 1111",
        "4111   1111   1111   1111",
        "4111 1111 1111 1111",
        "4111 1111 1111 1111",
        "4111​1111​1111​1111",  # zero-width space
        "４１１１１１１１１１１１１１１１",
        "⁴¹¹¹" + "1" * 12,  # superscript digits
        "4111%201111%201111%201111",
        "4111 1111/1111 1111",
        "3201.0101.9001.0001",
        "NIK 3201 0101 9001 0001",
        "0812 - 3456 - 7890",
        "0812/3456/7890",
        "+62 - 812 - 3456 - 7890",
        "＋62 812 3456 7890",
        "(+62) 812-3456-7890",
    ],
)
def test_numbers_are_removed_whatever_the_separator_or_script(text: str) -> None:
    out = redact(text)
    assert not re.search(r"\d{4}", out), out


@pytest.mark.parametrize(
    "text",
    [
        "john.doe at example dot com",
        "john.doe (at) example.com",
        "john.doe [at] example [dot] com",
        "john.doe AT example DOT co DOT id",
        "john.doe＠example.com",  # full-width at
        "john.doe﹫example.com",  # small commercial at
        "john.doe%40example.com",
        "john.doe&#64;example.com",
        "john.doe&commat;example.com",
    ],
)
def test_emails_are_removed_in_every_written_form(text: str) -> None:
    out = redact(f"mail {text} now")
    assert "john" not in out and "example" not in out, out


@pytest.mark.parametrize(
    "text",
    ["look at region", "meet at the branch", "arrive at 5.30pm", "what is at stake", "at"],
)
def test_the_word_at_alone_is_not_an_email(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "2026-06-30",
        "2026-06-30 12:00",
        "2026-06-30T12:00:00",
        "npf 2026-06-30 12:00 per cabang",
        "from 2025-10-01 to 2026-07-01",
    ],
)
def test_iso_dates_and_times_stay_readable(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "4111-1111-1111-1111",
        "2026-06-30-1234-5678",
        "2026-06-30 1234 5678 9",
        "12026-06-30 12:00 345",
        "2026-06-30 12:00 123456789",
        "1234 2026-06-30 123456",
        "2026-06-30 12:00:00 123 456 789",
    ],
)
def test_a_card_number_in_the_shape_of_a_date_is_still_caught(text: str) -> None:
    out = redact(text)
    assert REDACTED in out
    # Digits that went with the number are gone: none of the 9-digit run is left
    assert not re.search(
        r"\d(?:\D{0,3}\d){8}", re.sub(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?", "", out)
    )


def test_redaction_returns_the_normalised_text() -> None:
    assert redact("Jawa Barat") == "Jawa Barat"
    assert redact("ＮＰＦ") == "NPF"
    assert redact("a​b") == "ab"


def test_non_finite_and_huge_numbers_are_removed_not_raised() -> None:
    assert redact_value(float("inf")) == REDACTED
    assert redact_value(float("nan")) == REDACTED
    assert redact_value(-float("inf")) == REDACTED
    assert redact_value(10**30) == REDACTED
    assert redact_value(1e30) == REDACTED
    assert redact_value(2.5) == 2.5
    assert redact_value([float("nan"), 3]) == [REDACTED, 3]


@pytest.mark.parametrize(
    "text",
    [
        "2026-06-30  1234-5678",
        "2026-06-30\u20131234 5678",
        "2026-06-30 - 12345678",
        "12345678  2026-06-30",
        "2026-06-30, 2026-07-31",
        "2026-06-30\u20091234\u20095678",
        "1234 5678  2026-06-30 12:00",
    ],
)
def test_a_date_does_not_shelter_the_digits_next_to_it(text: str) -> None:
    out = redact(text)
    assert not re.search(r"\d{4}", out), out


@pytest.mark.parametrize(
    "text",
    [
        "x%00y",
        "%0a%00",
        "x%2500y",
        "a\ue000b",
        "\ue0000\ue001",
        "2026-06-30 \ue0000\ue001",
        "\U000f0001",
    ],
)
def test_no_control_or_private_use_character_survives_and_nothing_raises(text: str) -> None:
    out = redact(text)
    assert not re.search("[\x00-\x08\x0b\x0c\x0e-\x1f\ue000-\uf8ff\U000f0000-\U0010ffff]", out)


@pytest.mark.parametrize(
    "text",
    [
        "john%2540example.com",
        "john&amp;#64;example.com",
        "john%252540example.com",
        "4111%25201111%25201111%25201111",
    ],
)
def test_double_encoding_is_decoded_before_matching(text: str) -> None:
    out = redact(text)
    assert "john" not in out and "4111" not in out


# --- fix round 3 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "4111:1111:1111:1111",
        "4111|1111;1111'1111",
        "4111·1111*1111+1111",
        "2026-06-30:12345678",
        "4͏111 1111 1111 1111",  # combining grapheme joiner
        "4́111 1111 1111 1111",  # combining acute
        "4111\U000e0020 1111 1111 1111",  # tag character
        "4111￿1111￿1111￿1111",  # noncharacter
        "4111᠎1111⁪1111￺1111",
        "4111️1111️1111️1111",  # variation selector
    ],
)
def test_any_non_letter_separator_and_invisible_marks_do_not_hide_a_number(text: str) -> None:
    assert not re.search(r"\d{4}", redact(text)), redact(text)


@pytest.mark.parametrize(
    "text", ["2026-06-30 12:00", "from 2025-10-01 to 2026-07-01", "look at region"]
)
def test_the_sparing_cases_still_hold(text: str) -> None:
    assert redact(text) == text


def test_text_encoded_deeper_than_the_cap_is_redacted_whole() -> None:
    deep = "john" + "%25" * 6 + "40example.com"
    assert redact(deep) == REDACTED
    assert redact_value(deep) == REDACTED
    assert redact("x%" + "25" * 8 + "00") == REDACTED
    # within the cap it is decoded and matched
    assert "john" not in redact("john" + "%25" * 3 + "40example.com")
    assert redact("100%") == "100%" and redact("a%zzb") == "a%zzb"


def test_stored_text_is_clipped_after_normalisation() -> None:
    stored = redact_value("ﷺ" * 500)
    assert isinstance(stored, str) and len(stored) <= 500
    stored = redact_value("ﷺ" * 500, 50)
    assert isinstance(stored, str) and len(stored) <= 50
    assert len(str(redact_value(["ﬃ" * 500] * 3))) < 2000
