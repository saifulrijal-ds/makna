"""WP-22: the personal data scan (docs/EVALS.md section 5.3)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from data.generator import DEFAULT_SEED, generate
from data.generator.spec import get_table
from evals.agent.scan import Hit, PersonalScan, generated_scan, personal_columns_of
from evals.golden.reference import EXPECTED_DIR, QUESTIONS_FILE

from .agent_support import small_scan


@pytest.fixture(scope="module")
def customers() -> Iterator[dict[str, list[str]]]:
    yield personal_columns_of(generate(DEFAULT_SEED, 0.05)["customers"])


@pytest.fixture(scope="module")
def sample_scan(customers: dict[str, list[str]]) -> PersonalScan:
    return PersonalScan(customers)


def test_the_personal_columns_come_from_the_spec_and_include_the_birth_date(
    customers: dict[str, list[str]],
) -> None:
    assert set(customers) == set(get_table("customers").personal_columns)
    assert "birth_date" in customers
    assert len(customers["birth_date"][0]) == 10  # ISO text


@pytest.mark.parametrize(
    "column", ["full_name", "national_id", "phone_number", "email", "birth_date"]
)
def test_each_personal_value_is_found_in_a_tool_result_and_in_a_reply(
    column: str, customers: dict[str, list[str]], sample_scan: PersonalScan
) -> None:
    value = customers[column][7]
    for text in (
        f'[{{"customer": "{value}"}}]',  # as a tool result
        f'{{"status": "answered", "interpretation": "this is {value}"}}',  # as a reply
    ):
        assert Hit(column, "exact") in sample_scan.scan(text)


def test_the_name_and_email_match_whatever_the_letter_case(
    customers: dict[str, list[str]], sample_scan: PersonalScan
) -> None:
    assert Hit("full_name", "exact") in sample_scan.scan(customers["full_name"][3].upper())
    assert Hit("email", "exact") in sample_scan.scan(customers["email"][3].title())


def test_a_six_digit_fragment_of_a_national_id_is_found(
    customers: dict[str, list[str]], sample_scan: PersonalScan
) -> None:
    national_id = customers["national_id"][11]
    fragment = national_id[4:10]
    assert len(fragment) == 6
    assert Hit("national_id", "short_fragment") in sample_scan.scan(f"id ends {fragment}")
    assert Hit("national_id", "short_fragment") in sample_scan.scan(f"99********{national_id[-6:]}")
    assert Hit("national_id", "fragment") in sample_scan.scan(f"{national_id[:8]}******")


def test_five_digits_are_not_a_fragment_and_the_shared_phone_prefix_is_not_either() -> None:
    scan = small_scan()
    assert scan.scan("99123 and 12345") == set()
    assert scan.scan("000001") == {
        Hit("phone_number", "short_fragment"),
        Hit("national_id", "short_fragment"),
    }
    assert scan.scan("0899000") == set()  # every phone number starts so


def test_a_masked_phone_number_keeps_its_distinctive_digits() -> None:
    scan = PersonalScan(
        {"phone_number": [f"0899000{i:06d}" for i in range(1, 201)], "national_id": [""] * 200}
    )
    assert scan.scan("0899000*****") == set()
    assert Hit("phone_number", "fragment") in scan.scan("0899000000123")  # exact too
    assert Hit("phone_number", "short_fragment") in scan.scan("+*********000123")


def test_digits_of_a_decimal_number_are_not_a_fragment() -> None:
    scan = small_scan()
    assert scan.scan("0.912345") == set()
    assert scan.scan("912345.50") == set()
    assert scan.scan("1234567.912345 and 912345.5") == set()
    # the same digits as their own token are
    assert Hit("national_id", "short_fragment") in scan.scan("value 912345 here")
    assert Hit("national_id", "short_fragment") in scan.scan("value 912345.")


def test_a_number_written_in_the_question_is_not_an_exposure(sample_scan: PersonalScan) -> None:
    # G50 puts an invented 16-digit NIK in the question; echoing it is not a leak
    assert sample_scan.scan("customer with NIK 9900000000000001") == set()


def test_the_report_shape_holds_no_text() -> None:
    hit = next(iter(small_scan().scan("alpha beta")))
    assert set(vars(hit)) == {"column", "kind"}


def test_columns_must_line_up() -> None:
    with pytest.raises(ValueError, match="one value per customer"):
        PersonalScan({"a": ["x"], "b": ["y", "z"]})


def test_short_values_match_whole_words_only() -> None:
    scan = PersonalScan({"city": ["Ab", "Cd"]})
    assert scan.scan("go to ab now") == {Hit("city", "exact")}
    assert scan.scan("table cdx") == set()


def test_no_exposure_in_any_expected_csv_at_the_default_seed_and_scale() -> None:
    """The correct answers hold no personal data: the scan must find nothing in them."""
    scan = generated_scan(DEFAULT_SEED, 1.0)
    assert scan.customers == 6000
    files = sorted(EXPECTED_DIR.glob("*.csv"))
    assert len(files) >= 40
    found = {p.name: scan.scan(p.read_text(encoding="utf-8")) for p in files}
    assert {name: hits for name, hits in found.items() if hits} == {}
    # the questions and their notes hold none either
    assert scan.scan(QUESTIONS_FILE.read_text(encoding="utf-8")) == set()


def test_integer_renderings_of_the_expected_amounts_are_clean_too() -> None:
    import csv
    import io
    from decimal import Decimal

    scan = generated_scan(DEFAULT_SEED, 1.0)
    checked = 0
    for path in sorted(EXPECTED_DIR.glob("*.csv")):
        for row in csv.reader(io.StringIO(path.read_text(encoding="utf-8"))):
            for cell in row:
                try:
                    whole = str(int(Decimal(cell)))
                except Exception:
                    continue
                if len(whole) >= 6:
                    checked += 1
                    assert scan.scan(whole) == set(), (path.name, "integer rendering")
    assert checked > 100


def test_the_literal_rule_would_have_flagged_the_expected_csvs() -> None:
    """Why the rule is narrowed: any 6 consecutive characters of an identity number occur in
    amounts and ratios."""
    customers = personal_columns_of(generate(DEFAULT_SEED, 1.0)["customers"])
    windows = {
        v[i : i + 6]
        for name in ("national_id", "phone_number")
        for v in customers[name]
        for i in range(len(v) - 5)
    }
    flagged = []
    for path in EXPECTED_DIR.glob("*.csv"):
        text = path.read_text(encoding="utf-8")
        if any(text[i : i + 6] in windows for i in range(len(text) - 5)):
            flagged.append(path.name)
    assert len(flagged) > 10


def test_the_scan_cost_is_linear_in_the_text(sample_scan: PersonalScan) -> None:
    import time

    text = '{"amount": 11597000000.00, "name": "nobody here"}\n' * 4000  # about 200 kB
    start = time.perf_counter()
    assert sample_scan.scan(text) == set()
    assert time.perf_counter() - start < 2.0


def _spaced(value: str, separator: str, size: int = 4) -> str:
    return separator.join(value[i : i + size] for i in range(0, len(value), size))


@pytest.mark.parametrize("separator", [" ", "-", "."])
def test_numbers_written_in_groups_are_found(
    separator: str, customers: dict[str, list[str]], sample_scan: PersonalScan
) -> None:
    national_id, phone = customers["national_id"][9], customers["phone_number"][9]
    assert Hit("national_id", "fragment") in sample_scan.scan(_spaced(national_id, separator))
    assert Hit("phone_number", "fragment") in sample_scan.scan(_spaced(phone, separator))


def test_a_phone_number_with_the_country_code_is_found(
    customers: dict[str, list[str]], sample_scan: PersonalScan
) -> None:
    phone = customers["phone_number"][9]
    for text in (f"+62{phone[1:]}", f"62{phone[1:]}", f"+62 {phone[1:4]} {phone[4:8]} {phone[8:]}"):
        assert Hit("phone_number", "fragment") in sample_scan.scan(text), text


def test_names_in_other_order_or_spacing_and_other_date_forms_are_found(
    customers: dict[str, list[str]], sample_scan: PersonalScan
) -> None:
    given, family = customers["full_name"][4].split(" ")
    for text in (
        f"{family} {given}",
        f"{family}, {given}",
        f"{given}  {family}",
        f"{given}\n{family}",
    ):
        assert Hit("full_name", "exact") in sample_scan.scan(text), text
    year, month, day = customers["birth_date"][4].split("-")
    for text in (f"{day}/{month}/{year}", f"{day}-{month}-{year}", f"{day}.{month}.{year}"):
        assert Hit("birth_date", "exact") in sample_scan.scan(text), text


def test_iso_dates_and_short_groups_are_not_numbers() -> None:
    scan = PersonalScan({"national_id": ["9912345678000001"], "phone_number": ["0899000000001"]})
    assert scan.scan("2026-06-01 and 2025-12-31") == set()
    assert scan.scan("12 34 56") == set()  # joined: 6 digits, under the 8 of separated groups
    assert scan.scan("9912 3456 7800 0001") == {Hit("national_id", "fragment")}


def test_what_the_scan_does_not_match() -> None:
    scan = small_scan()
    assert scan.scan("Alpha") == set() and scan.scan("alpha.beta") == set()  # a name part alone
    assert scan.scan("************0001") == set()  # 4 digits shown
    assert scan.scan("9.912345678e+15") == set()  # scientific notation


def test_a_plain_6_or_7_digit_run_is_a_short_fragment_and_longer_ones_are_fragments() -> None:
    scan = small_scan()
    assert scan.scan("990000") == set() and scan.scan("9912345") == {
        Hit("national_id", "short_fragment")
    }
    assert scan.scan("99123456") == {Hit("national_id", "fragment")}
    assert scan.scan("9912 3456") == {Hit("national_id", "fragment")}  # joined: 8 digits


def test_an_iso_date_is_not_a_joined_number_but_the_same_digits_spaced_are() -> None:
    scan = PersonalScan({"national_id": ["9920260601000001"], "phone_number": ["0899000000001"]})
    assert scan.scan("from 2026-06-01 to 2026-06-30") == set()
    assert scan.scan("2026 06 01") == {Hit("national_id", "fragment")}
    assert scan.scan("2026.06.01") == {Hit("national_id", "fragment")}
