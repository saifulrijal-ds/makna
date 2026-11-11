"""Reference and origination entities: branches, products, customers, applications."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from . import reference as ref

IntArray = NDArray[np.int64]
ORIGINATION_SKEW = 2.0
"""Volumes are front-loaded (a declining book): a power above 1 puts more records early.
This keeps the child-table sizes near the targets of docs/SAMPLE_DOMAIN.md section 2."""
Columns = dict[str, Sequence[object]]


def exact_allocation(rng: np.random.Generator, n: int, shares: tuple[float, ...]) -> IntArray:
    """Assign each of `n` items to a class so class sizes match `shares` as closely as possible.

    Sizes are fixed by largest remainder; only which item gets which class is random. Quirk
    rates therefore hold at every scale instead of drifting with sampling noise.
    """
    raw = np.array(shares, dtype=np.float64) / sum(shares) * n
    counts = np.floor(raw).astype(np.int64)
    order = np.argsort(-(raw - counts), kind="stable")
    for i in range(n - int(counts.sum())):
        counts[order[i]] += 1
    labels = np.repeat(np.arange(len(shares), dtype=np.int64), counts)
    rng.shuffle(labels)
    return labels


@dataclass
class Branches:
    columns: Columns
    count: int
    cities: list[str]
    provinces: list[str]
    areas: list[str]
    is_active: NDArray[np.bool_]
    weights: NDArray[np.float64]


def build_branches(rng: np.random.Generator) -> Branches:
    ids: list[object] = []
    codes: list[object] = []
    names: list[object] = []
    areas: list[object] = []
    regions: list[object] = []
    provinces: list[object] = []
    cities: list[str] = []
    for area in ref.AREAS:
        for city in area.cities:
            for seq in range(1, city.branches + 1):
                ids.append(len(ids) + 1)
                codes.append(f"{city.code}{seq:02d}")
                names.append(f"{city.city} {seq}" if city.branches > 1 else city.city)
                areas.append(area.area)
                regions.append(area.region)
                provinces.append(city.province)
                cities.append(city.city)
    n = len(ids)
    # Closed branches are drawn from cities that have several branches.
    multi = [i for i in range(n) if cities.count(cities[i]) > 1]
    closed = set(rng.choice(multi, size=ref.CLOSED_BRANCH_COUNT, replace=False).tolist())
    opened_days = rng.integers(
        ref.to_day(ref.START_DATE) - 16 * 365, ref.to_day(ref.START_DATE) - 90, size=n
    )
    is_active = np.array([i not in closed for i in range(n)], dtype=np.bool_)
    weights = rng.lognormal(0.0, 0.5, size=n)
    columns: Columns = {
        "branch_id": ids,
        "branch_code": codes,
        "branch_name": names,
        "area_name": areas,
        "region_name": regions,
        "province": provinces,
        "opened_date": [int(d) for d in opened_days],
        "is_active": [bool(a) for a in is_active],
    }
    return Branches(
        columns, n, cities, [str(p) for p in provinces], [str(a) for a in areas], is_active, weights
    )


@dataclass
class Products:
    columns: Columns
    count: int
    categories: list[str]
    weights: NDArray[np.float64]


def build_products() -> Products:
    columns: Columns = {
        "product_id": [i + 1 for i in range(len(ref.PRODUCTS))],
        "product_code": [p.code for p in ref.PRODUCTS],
        "product_name": [p.name for p in ref.PRODUCTS],
        "product_category": [p.category for p in ref.PRODUCTS],
        "financing_scheme": [p.scheme for p in ref.PRODUCTS],
    }
    return Products(
        columns,
        len(ref.PRODUCTS),
        [p.category for p in ref.PRODUCTS],
        np.array([p.weight for p in ref.PRODUCTS]),
    )


def _choice(
    rng: np.random.Generator, options: tuple[str, ...], weights: tuple[float, ...], size: int
) -> list[str]:
    idx = rng.choice(len(options), size=size, p=np.array(weights) / sum(weights))
    return [options[i] for i in idx]


def _syllable_word(rng: np.random.Generator) -> str:
    n = int(rng.integers(2, 4))
    parts = [ref.GIVEN_SYLLABLES[i] for i in rng.integers(0, len(ref.GIVEN_SYLLABLES), size=n)]
    parts.append(ref.NAME_ENDINGS[int(rng.integers(0, len(ref.NAME_ENDINGS)))])
    return "".join(parts).capitalize()


@dataclass
class Customers:
    columns: Columns
    count: int
    registered_second: IntArray
    home_branch_index: IntArray
    """Zero-based index into the branches."""


def _branch_choice(
    rng: np.random.Generator, branches: Branches, day: IntArray, size: int
) -> IntArray:
    """Weighted branch choice; closed branches take no new records from the cutoff date."""
    out = np.zeros(size, dtype=np.int64)
    p_all = branches.weights / branches.weights.sum()
    w_active = np.where(branches.is_active, branches.weights, 0.0)
    p_active = w_active / w_active.sum()
    early = day < ref.CLOSURE_CUTOFF_DAY
    n_early = int(early.sum())
    out[early] = rng.choice(branches.count, size=n_early, p=p_all)
    out[~early] = rng.choice(branches.count, size=size - n_early, p=p_active)
    return out


def build_customers(rng: np.random.Generator, n: int, branches: Branches) -> Customers:
    span = ref.ASOF_END_SECOND - ref.START_SECOND
    u = rng.random(n) ** ORIGINATION_SKEW
    registered = np.sort((ref.START_SECOND + u * (span - 1)).astype(np.int64))
    reg_day = registered // ref.SECONDS_PER_DAY
    home = _branch_choice(rng, branches, reg_day, n)

    gender = _choice(rng, ("F", "M"), (0.48, 0.52), n)
    occupation = _choice(rng, ref.OCCUPATIONS, ref.OCCUPATION_WEIGHTS, n)
    income = [""] * n
    for occ in ref.OCCUPATIONS:
        idx = [i for i in range(n) if occupation[i] == occ]
        for i, value in zip(
            idx,
            _choice(rng, ref.INCOME_BANDS, ref.INCOME_BY_OCCUPATION[occ], len(idx)),
            strict=True,
        ):
            income[i] = value
    segment = [""] * n
    for band in ref.INCOME_BANDS:
        idx = [i for i in range(n) if income[i] == band]
        for i, value in zip(
            idx, _choice(rng, ref.SEGMENTS, ref.SEGMENT_BY_INCOME[band], len(idx)), strict=True
        ):
            segment[i] = value

    other_branch = rng.integers(0, branches.count, size=n)
    use_home_city = rng.random(n) < 0.85
    city_index = np.where(use_home_city, home, other_branch)

    age_days = rng.integers(21 * 365 + 5, 66 * 365, size=n)
    birth_day = reg_day - age_days
    id_noise = rng.integers(0, 10**8, size=n)

    full_name: list[object] = []
    email: list[object] = []
    for i in range(n):
        given, family = _syllable_word(rng), _syllable_word(rng)
        full_name.append(f"{given} {family}")
        email.append(f"{given.lower()}.{family.lower()}{i + 1}@example.com")
    columns: Columns = {
        "customer_id": [i + 1 for i in range(n)],
        "full_name": full_name,
        "national_id": [f"99{int(id_noise[i]):08d}{i + 1:06d}" for i in range(n)],
        "phone_number": [f"0899000{i + 1:06d}" for i in range(n)],
        "email": email,
        "birth_date": [int(d) for d in birth_day],
        "gender": list(gender),
        "occupation_group": list(occupation),
        "income_band": income,
        "city": [branches.cities[int(i)] for i in city_index],
        "province": [branches.provinces[int(i)] for i in city_index],
        "customer_segment": segment,
        "home_branch_id": [int(h) + 1 for h in home],
        "registered_at": [int(s) for s in registered],
    }
    return Customers(columns, n, registered, home)


@dataclass
class Applications:
    columns: Columns
    count: int
    # Arrays used by the lifecycle (index = application id - 1).
    customer_index: IntArray
    product_index: IntArray
    branch_index: IntArray
    status: list[str]
    decision_day: IntArray
    approved_amount: IntArray
    tenor: IntArray


def _round_to(values: NDArray[np.float64], step: int) -> IntArray:
    return (np.round(values / step) * step).astype(np.int64)


def build_applications(
    rng: np.random.Generator,
    n: int,
    customers: Customers,
    branches: Branches,
    products: Products,
) -> Applications:
    n_cust = customers.count
    if n < n_cust:
        raise ValueError("there must be at least one application per customer")
    last_second = ref.ASOF_END_SECOND - 1
    # Every customer applies once, shortly after registering; the rest are repeat applications.
    first = np.minimum(
        customers.registered_second + rng.integers(0, 3 * ref.SECONDS_PER_DAY, size=n_cust),
        last_second,
    )
    cust_idx = np.arange(n_cust, dtype=np.int64)
    submitted = first
    n_repeat = n - n_cust
    if n_repeat:
        eligible = np.flatnonzero(
            customers.registered_second + 30 * ref.SECONDS_PER_DAY < last_second
        )
        repeat_cust = rng.choice(eligible, size=n_repeat)
        low = customers.registered_second[repeat_cust] + 30 * ref.SECONDS_PER_DAY
        repeat_sub = low + (
            (rng.random(n_repeat) ** ORIGINATION_SKEW) * (last_second - low)
        ).astype(np.int64)
        cust_idx = np.concatenate([cust_idx, repeat_cust])
        submitted = np.concatenate([submitted, repeat_sub])
    order = np.argsort(submitted, kind="stable")
    cust_idx = cust_idx[order]
    submitted = submitted[order]
    sub_day = submitted // ref.SECONDS_PER_DAY

    prod_idx = rng.choice(products.count, size=n, p=products.weights / products.weights.sum())
    home = customers.home_branch_index[cust_idx]
    alt = _branch_choice(rng, branches, sub_day, n)
    home_closed_now = ~branches.is_active[home] & (sub_day >= ref.CLOSURE_CUTOFF_DAY)
    use_home = (rng.random(n) < 0.8) & ~home_closed_now
    branch_idx = np.where(use_home, home, alt)

    channel = _choice(rng, ref.CHANNELS, ref.CHANNEL_WEIGHTS, n)

    lows = np.array([p.amount_low for p in ref.PRODUCTS], dtype=np.float64)[prod_idx]
    highs = np.array([p.amount_high for p in ref.PRODUCTS], dtype=np.float64)[prod_idx]
    requested = _round_to(lows + rng.random(n) * (highs - lows), 500_000)
    requested = np.maximum(requested, 1_000_000)

    tenor = np.zeros(n, dtype=np.int64)
    for category, (options, weights) in ref.TENORS_BY_CATEGORY.items():
        mask = np.array([products.categories[p] == category for p in prod_idx])
        k = int(mask.sum())
        tenor[mask] = np.array(options)[rng.choice(len(options), size=k, p=weights)]

    status_class = exact_allocation(rng, n, ref.APPLICATION_STATUS_SHARES)
    status = [ref.APPLICATION_STATUSES[c] for c in status_class]
    lag = np.where(status_class == 0, rng.integers(0, 6, size=n), rng.integers(0, 5, size=n))
    decision_day = sub_day + lag
    for i in range(n):
        if status[i] in ("approved", "rejected") and decision_day[i] > ref.ASOF_DAY:
            status[i] = "in_process"

    has_decision = np.array([s in ("approved", "rejected") for s in status])
    grade: list[object] = [None] * n
    approved_grades = _choice(rng, ref.GRADES, ref.APPROVED_GRADE_WEIGHTS, n)
    rejected_grades = _choice(rng, ref.GRADES, ref.REJECTED_GRADE_WEIGHTS, n)
    for i in range(n):
        if status[i] == "approved":
            grade[i] = approved_grades[i]
        elif status[i] == "rejected":
            grade[i] = rejected_grades[i]

    reduced = rng.random(n) < 0.45
    factor = rng.uniform(0.6, 0.95, size=n)
    approved_amt = np.where(reduced, _round_to(requested * factor, 500_000), requested)
    approved_amt = np.clip(approved_amt, 1_000_000, requested)

    is_approved = np.array([s == "approved" for s in status])
    columns: Columns = {
        "appl_id": [i + 1 for i in range(n)],
        "cust_id": [int(c) + 1 for c in cust_idx],
        "prod_id": [int(p) + 1 for p in prod_idx],
        "cab_id": [int(b) + 1 for b in branch_idx],
        "saluran": list(channel),
        "tgl_pengajuan": [int(s) for s in submitted],
        "tgl_putusan": [int(decision_day[i]) if has_decision[i] else None for i in range(n)],
        "sts_putusan": list(status),
        "req_amt": [int(a) for a in requested],
        "appr_amt": [int(approved_amt[i]) if is_approved[i] else None for i in range(n)],
        "req_tenor": [int(t) for t in tenor],
        "grade_risiko": grade,
    }
    return Applications(
        columns, n, cust_idx, prod_idx, branch_idx, status, decision_day, approved_amt, tenor
    )
