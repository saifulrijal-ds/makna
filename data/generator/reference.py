"""Static reference data for the generator: branch layout, products, and value lists.

Everything here is invented. Place names are real public geography used only as labels for
fictional branches; no person, company or account data appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

EPOCH_ORDINAL = date(1970, 1, 1).toordinal()

START_DATE = date(2024, 1, 1)
ASOF_DATE = date(2026, 6, 30)
"""The 'as of' date of the dataset (docs/SAMPLE_DOMAIN.md section 2)."""
BRANCH_CLOSURE_CUTOFF = date(2025, 1, 1)
"""Closed branches take no new applications from this date."""

START_DAY = START_DATE.toordinal() - EPOCH_ORDINAL
ASOF_DAY = ASOF_DATE.toordinal() - EPOCH_ORDINAL
CLOSURE_CUTOFF_DAY = BRANCH_CLOSURE_CUTOFF.toordinal() - EPOCH_ORDINAL
SECONDS_PER_DAY = 86_400
START_SECOND = START_DAY * SECONDS_PER_DAY
ASOF_END_SECOND = (ASOF_DAY + 1) * SECONDS_PER_DAY


def to_day(d: date) -> int:
    """Days since 1970-01-01 (the Parquet date32 representation)."""
    return d.toordinal() - EPOCH_ORDINAL


def from_day(n: int) -> date:
    return date.fromordinal(n + EPOCH_ORDINAL)


@dataclass(frozen=True)
class CitySpec:
    code: str
    city: str
    province: str
    branches: int


@dataclass(frozen=True)
class AreaSpec:
    area: str
    region: str
    cities: tuple[CitySpec, ...]


# 12 areas, 5 regions, 40 branches. Region is a sales territory; province is administrative
# (Q10): region "Jawa Barat" spans the provinces Jawa Barat, DKI Jakarta and Banten.
AREAS: tuple[AreaSpec, ...] = (
    AreaSpec(
        "Bandung Raya",
        "Jawa Barat",
        (CitySpec("BDG", "Bandung", "Jawa Barat", 3), CitySpec("CMH", "Cimahi", "Jawa Barat", 1)),
    ),
    AreaSpec(
        "Bodetabek",
        "Jawa Barat",
        (
            CitySpec("BKS", "Bekasi", "Jawa Barat", 1),
            CitySpec("BGR", "Bogor", "Jawa Barat", 2),
            CitySpec("DPK", "Depok", "Jawa Barat", 1),
        ),
    ),
    AreaSpec(
        "Jakarta Banten",
        "Jawa Barat",
        (CitySpec("JKT", "Jakarta", "DKI Jakarta", 3), CitySpec("TGR", "Tangerang", "Banten", 2)),
    ),
    AreaSpec(
        "Semarang Raya",
        "Jawa Tengah",
        (CitySpec("SMG", "Semarang", "Jawa Tengah", 3), CitySpec("KDS", "Kudus", "Jawa Tengah", 1)),
    ),
    AreaSpec(
        "Solo Yogya",
        "Jawa Tengah",
        (
            CitySpec("SKA", "Surakarta", "Jawa Tengah", 2),
            CitySpec("YGY", "Yogyakarta", "DI Yogyakarta", 2),
        ),
    ),
    AreaSpec(
        "Surabaya Raya",
        "Jawa Timur",
        (
            CitySpec("SBY", "Surabaya", "Jawa Timur", 3),
            CitySpec("SDA", "Sidoarjo", "Jawa Timur", 1),
        ),
    ),
    AreaSpec(
        "Malang Bali",
        "Jawa Timur",
        (CitySpec("MLG", "Malang", "Jawa Timur", 2), CitySpec("DPS", "Denpasar", "Bali", 1)),
    ),
    AreaSpec("Sumatera Utara", "Sumatera", (CitySpec("MDN", "Medan", "Sumatera Utara", 3),)),
    AreaSpec(
        "Sumatera Tengah",
        "Sumatera",
        (CitySpec("PDG", "Padang", "Sumatera Barat", 1), CitySpec("PKU", "Pekanbaru", "Riau", 1)),
    ),
    AreaSpec(
        "Sumatera Selatan",
        "Sumatera",
        (
            CitySpec("PLG", "Palembang", "Sumatera Selatan", 1),
            CitySpec("BDL", "Bandar Lampung", "Lampung", 1),
        ),
    ),
    AreaSpec(
        "Kalimantan",
        "Kalimantan Sulawesi",
        (
            CitySpec("BPN", "Balikpapan", "Kalimantan Timur", 1),
            CitySpec("BJM", "Banjarmasin", "Kalimantan Selatan", 1),
        ),
    ),
    AreaSpec(
        "Sulawesi",
        "Kalimantan Sulawesi",
        (
            CitySpec("UPG", "Makassar", "Sulawesi Selatan", 2),
            CitySpec("MDC", "Manado", "Sulawesi Utara", 1),
        ),
    ),
)
CLOSED_BRANCH_COUNT = 2


@dataclass(frozen=True)
class ProductSpec:
    code: str
    name: str
    category: str
    scheme: str
    rate_low: float
    rate_high: float
    amount_low: int
    amount_high: int
    weight: float


MILLION = 1_000_000

PRODUCTS: tuple[ProductSpec, ...] = (
    ProductSpec(
        "CBL-CV",
        "Car Backed Loan",
        "car_backed",
        "conventional",
        9.0,
        13.5,
        50 * MILLION,
        400 * MILLION,
        0.12,
    ),
    ProductSpec(
        "CBL-SH",
        "Car Backed Loan Sharia",
        "car_backed",
        "sharia",
        9.5,
        13.5,
        50 * MILLION,
        400 * MILLION,
        0.06,
    ),
    ProductSpec(
        "MBL-CV",
        "Motorcycle Backed Loan",
        "motorcycle_backed",
        "conventional",
        14.0,
        20.0,
        5 * MILLION,
        40 * MILLION,
        0.24,
    ),
    ProductSpec(
        "MBL-SH",
        "Motorcycle Backed Loan Sharia",
        "motorcycle_backed",
        "sharia",
        14.0,
        20.0,
        5 * MILLION,
        40 * MILLION,
        0.10,
    ),
    ProductSpec(
        "UCP-CV",
        "Used Car Purchase",
        "used_car_purchase",
        "conventional",
        10.0,
        15.0,
        80 * MILLION,
        350 * MILLION,
        0.14,
    ),
    ProductSpec(
        "UCP-SH",
        "Used Car Purchase Sharia",
        "used_car_purchase",
        "sharia",
        10.0,
        15.0,
        80 * MILLION,
        350 * MILLION,
        0.06,
    ),
    ProductSpec(
        "UNS-CV",
        "Personal Cash Loan",
        "unsecured",
        "conventional",
        20.0,
        28.0,
        3 * MILLION,
        50 * MILLION,
        0.20,
    ),
    ProductSpec(
        "UNS-SH",
        "Personal Cash Financing Sharia",
        "unsecured",
        "sharia",
        20.0,
        28.0,
        3 * MILLION,
        50 * MILLION,
        0.08,
    ),
)

TENORS_BY_CATEGORY: dict[str, tuple[tuple[int, ...], tuple[float, ...]]] = {
    "car_backed": ((12, 18, 24, 36, 48), (0.10, 0.15, 0.25, 0.30, 0.20)),
    "used_car_purchase": ((12, 18, 24, 36, 48), (0.10, 0.15, 0.25, 0.30, 0.20)),
    "motorcycle_backed": ((12, 18, 24, 36), (0.25, 0.25, 0.30, 0.20)),
    "unsecured": ((12, 18, 24, 36), (0.35, 0.30, 0.25, 0.10)),
}

CHANNELS = ("walk_in", "agent", "online", "telesales")
CHANNEL_WEIGHTS = (0.30, 0.25, 0.25, 0.20)

# Share of applications by final decision status; in_process arises only from timing.
APPLICATION_STATUSES = ("approved", "rejected", "cancelled")
APPLICATION_STATUS_SHARES = (0.62, 0.27, 0.11)

GRADES = ("A", "B", "C", "D", "E")
APPROVED_GRADE_WEIGHTS = (0.30, 0.30, 0.22, 0.12, 0.06)
REJECTED_GRADE_WEIGHTS = (0.00, 0.05, 0.15, 0.30, 0.50)

OCCUPATIONS = ("employee", "self_employed", "civil_servant", "professional", "other")
OCCUPATION_WEIGHTS = (0.45, 0.25, 0.10, 0.08, 0.12)
INCOME_BANDS = ("lt_5m", "5m_10m", "10m_20m", "gt_20m")
INCOME_BY_OCCUPATION: dict[str, tuple[float, ...]] = {
    "employee": (0.30, 0.40, 0.25, 0.05),
    "self_employed": (0.30, 0.30, 0.25, 0.15),
    "civil_servant": (0.20, 0.45, 0.30, 0.05),
    "professional": (0.05, 0.20, 0.45, 0.30),
    "other": (0.60, 0.30, 0.08, 0.02),
}
SEGMENTS = ("mass", "emerging_affluent", "affluent")
SEGMENT_BY_INCOME: dict[str, tuple[float, ...]] = {
    "lt_5m": (1.00, 0.00, 0.00),
    "5m_10m": (0.85, 0.15, 0.00),
    "10m_20m": (0.35, 0.55, 0.10),
    "gt_20m": (0.05, 0.35, 0.60),
}

# Invented syllables: names built from them are clearly synthetic.
GIVEN_SYLLABLES = (
    "ba",
    "da",
    "ka",
    "la",
    "ma",
    "na",
    "ra",
    "sa",
    "ta",
    "wi",
    "ru",
    "li",
    "mo",
    "ne",
    "ki",
    "to",
)
NAME_ENDINGS = ("", "", "n", "r", "l", "m", "s", "k", "ng")

# Customer behaviour classes (Q5): on time, late, very late then recover, stop paying.
BEHAVIOUR_SHARES = (0.70, 0.18, 0.07, 0.05)
PAYMENT_CHANNELS = (
    "bank_transfer",
    "virtual_account",
    "retail_outlet",
    "branch_cashier",
    "collector",
)
PAYMENT_CHANNEL_WEIGHTS = (0.25, 0.35, 0.20, 0.10, 0.10)
