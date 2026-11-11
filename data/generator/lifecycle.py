"""Contract lifecycle: contracts, schedules, payments, collection actions and snapshots.

Each disbursed contract follows one payment behaviour (Q5). Payments are simulated against the
schedule; the month-end snapshot table is then derived from schedules and payments exactly as
docs/SAMPLE_DOMAIN.md ("How the snapshot table is derived") describes, so the tables agree.

Money is kept in whole rupiah (the Parquet columns are decimal(18,2)); the flat annual rate is
kept in hundredths of a percent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
from numpy.typing import NDArray

from . import reference as ref
from .entities import Applications, Branches, Columns, IntArray, exact_allocation

INF = 10**9
WRITE_OFF_DPD = 210
GRACE_DAYS = 3
REVERSAL_PROBABILITY = 0.0309
"""Chosen so that about 3% of all payment rows are reversed (Q6), replacements included."""
ACTION_PROBABILITY = 0.275
MAX_ACTIONS_PER_INSTALLMENT = 3

CANCELLED_SHARE = 0.04
TRANSFERRED_SHARE = 0.20
RESTRUCTURED_SHARE = 0.09

ON_TIME, LATE, VERY_LATE, STOPPED = 0, 1, 2, 3


def _month_ends() -> IntArray:
    out: list[int] = []
    year, month = ref.START_DATE.year, ref.START_DATE.month
    while date(year, month, 1) <= ref.ASOF_DATE:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        out.append(ref.to_day(date(year, month, 1)) - 1)
    return np.array(out, dtype=np.int64)


MONTH_ENDS = _month_ends()


def _due_day(year: int, month: int, dom: int, k: int) -> int:
    """Due date of installment `k` for a contract disbursed in (year, month), as a day number."""
    index = year * 12 + (month - 1) + k
    return ref.to_day(date(index // 12, index % 12 + 1, dom))


def _split(total: int, parts: int) -> list[int]:
    """Split a whole-rupiah amount into `parts` pieces; the last one absorbs the remainder."""
    each = total // parts
    pieces = [each] * parts
    pieces[-1] += total - each * parts
    return pieces


# Collection actions: type by days overdue (bins <=7, <=30, <=60, more), outcome by type.
ACTION_TYPES = ("reminder_message", "phone_call", "field_visit", "warning_letter")
_TYPE_WEIGHTS = np.array(
    [
        [0.80, 0.20, 0.00, 0.00],
        [0.20, 0.60, 0.20, 0.00],
        [0.00, 0.35, 0.45, 0.20],
        [0.00, 0.20, 0.40, 0.40],
    ]
)
ACTION_OUTCOMES = ("promise_to_pay", "paid", "no_contact", "refused")
_OUTCOME_WEIGHTS = np.array(
    [
        [0.25, 0.10, 0.55, 0.10],
        [0.40, 0.15, 0.30, 0.15],
        [0.35, 0.20, 0.25, 0.20],
        [0.20, 0.10, 0.50, 0.20],
    ]
)
_TYPE_CUM = np.cumsum(_TYPE_WEIGHTS, axis=1)[:, :-1]
_OUTCOME_CUM = np.cumsum(_OUTCOME_WEIGHTS, axis=1)[:, :-1]
_CHANNEL_P = np.array(ref.PAYMENT_CHANNEL_WEIGHTS) / sum(ref.PAYMENT_CHANNEL_WEIGHTS)


@dataclass
class Lifecycle:
    contracts: Columns
    schedules: Columns
    payments: Columns
    actions: Columns
    snapshots: Columns


class _Out:
    """Row accumulators for the child tables."""

    def __init__(self) -> None:
        self.sched: dict[str, list[Any]] = {
            k: []
            for k in (
                "contract_id",
                "installment_no",
                "schedule_version",
                "is_current",
                "effective_from",
                "superseded_date",
                "due_date",
                "principal_due",
                "interest_due",
                "installment_amount",
            )
        }
        self.pay: dict[str, list[Any]] = {
            k: []
            for k in (
                "contract_id",
                "schedule_id",
                "payment_date",
                "posting_date",
                "principal_paid",
                "interest_paid",
                "penalty_paid",
                "payment_channel",
                "is_reversed",
            )
        }
        self.act: dict[str, list[Any]] = {
            k: []
            for k in (
                "kontrak_id",
                "tgl_aksi",
                "jns_aksi",
                "kolektor_id",
                "hasil",
                "tgl_janji",
                "nom_janji",
            )
        }
        self.snap: dict[str, list[Any]] = {
            k: []
            for k in ("snapshot_date", "contract_id", "outstanding_principal", "days_past_due")
        }
        self.n_schedule_rows = 0


def build_lifecycle(rng: np.random.Generator, apps: Applications, branches: Branches) -> Lifecycle:
    approved = [i for i, s in enumerate(apps.status) if s == "approved"]
    sign_lag = rng.integers(1, 8, size=len(approved))
    sign_all = apps.decision_day[approved] + sign_lag
    # A contract signed in the last days could not have been disbursed by the as-of date.
    keep = sign_all <= ref.ASOF_DAY - 5
    app_idx = np.array(approved, dtype=np.int64)[keep]
    sign_day = sign_all[keep]
    order = np.lexsort((app_idx, sign_day))
    app_idx = app_idx[order]
    sign_day = sign_day[order]
    n = len(app_idx)

    cancelled = exact_allocation(rng, n, (CANCELLED_SHARE, 1 - CANCELLED_SHARE)) == 0
    disb_day = sign_day + rng.integers(0, 6, size=n)

    prod_idx = apps.product_index[app_idx]
    rate_low = np.array([p.rate_low for p in ref.PRODUCTS])[prod_idx]
    rate_high = np.array([p.rate_high for p in ref.PRODUCTS])[prod_idx]
    rate_h = (np.round((rate_low + rng.random(n) * (rate_high - rate_low)) * 4) * 25).astype(
        np.int64
    )

    booking = apps.branch_index[app_idx]
    servicing = _servicing_branches(rng, booking, cancelled, branches)

    disbursed = np.flatnonzero(~cancelled)
    classes = np.full(n, -1, dtype=np.int64)
    classes[disbursed] = exact_allocation(rng, len(disbursed), ref.BEHAVIOUR_SHARES)

    # Phase A: due dates, and which contracts are restructured and when.
    dues: dict[int, IntArray] = {}
    elapsed: dict[int, int] = {}
    for c in disbursed.tolist():
        d = ref.from_day(int(disb_day[c]))
        dom = min(d.day, 28)
        tenor = int(apps.tenor[app_idx[c]])
        due = np.array(
            [_due_day(d.year, d.month, dom, k) for k in range(1, tenor + 1)], dtype=np.int64
        )
        dues[c] = due
        elapsed[c] = int(np.searchsorted(due, ref.ASOF_DAY, side="right"))
    restructure: dict[int, tuple[int, int]] = {}
    eligible = [
        c
        for c in disbursed.tolist()
        if classes[c] != STOPPED
        and len(dues[c]) >= 12
        and elapsed[c] >= 5
        and min(elapsed[c], len(dues[c]) - 3) >= 3
    ]
    want = min(round(RESTRUCTURED_SHARE * len(disbursed)), len(eligible))
    for c in sorted(rng.choice(eligible, size=want, replace=False).tolist()) if want else []:
        n_inst = len(dues[c])
        j = int(rng.integers(3, min(elapsed[c], n_inst - 3) + 1))
        restructure[c] = (j, min(int(dues[c][j - 1]) + int(rng.integers(1, 21)), ref.ASOF_DAY))

    out = _Out()
    status: list[str] = [""] * n
    closed_day: list[int | None] = [None] * n
    maturity: list[int | None] = [None] * n
    first_due: list[int | None] = [None] * n
    restructured_on: list[int | None] = [None] * n
    for c in disbursed.tolist():
        principal = int(apps.approved_amount[app_idx[c]])
        d = ref.from_day(int(disb_day[c]))
        result = _simulate_contract(
            rng,
            out,
            contract_id=c + 1,
            servicing_index=int(servicing[c]),
            principal=principal,
            rate_h=int(rate_h[c]),
            disb_day=int(disb_day[c]),
            year=d.year,
            month=d.month,
            dom=min(d.day, 28),
            dues=dues[c],
            behaviour=int(classes[c]),
            restructure=restructure.get(c),
        )
        status[c], closed_day[c], maturity[c], first_due[c] = result
        if c in restructure:
            restructured_on[c] = restructure[c][1]
    for c in np.flatnonzero(cancelled).tolist():
        status[c] = "cancelled"

    contracts: Columns = {
        "contract_id": [i + 1 for i in range(n)],
        "application_id": [int(a) + 1 for a in app_idx],
        "customer_id": [int(apps.customer_index[a]) + 1 for a in app_idx],
        "product_id": [int(p) + 1 for p in prod_idx],
        "booking_branch_id": [int(b) + 1 for b in booking],
        "servicing_branch_id": [int(s) + 1 for s in servicing],
        "signed_date": [int(d) for d in sign_day],
        "disbursement_date": [None if cancelled[i] else int(disb_day[i]) for i in range(n)],
        "first_due_date": first_due,
        "maturity_date": maturity,
        "closed_date": closed_day,
        "principal_amount": [int(apps.approved_amount[a]) for a in app_idx],
        "tenor_months": [int(apps.tenor[a]) for a in app_idx],
        "annual_rate_pct": [int(r) for r in rate_h],
        "contract_status": status,
        "is_restructured": [i in restructure for i in range(n)],
        "restructured_date": restructured_on,
    }
    return Lifecycle(
        contracts=contracts,
        schedules=_schedule_columns(out),
        payments=_payment_columns(out),
        actions=_action_columns(out),
        snapshots=_snapshot_columns(out),
    )


def _servicing_branches(
    rng: np.random.Generator, booking: IntArray, cancelled: NDArray[np.bool_], branches: Branches
) -> IntArray:
    """About 20% of disbursed contracts are serviced by another branch than the booking one (Q3).

    Contracts booked at a closed branch are always transferred.
    """
    servicing = booking.copy()
    forced = ~branches.is_active[booking] & ~cancelled
    candidates = np.flatnonzero(~cancelled & ~forced)
    n_disbursed = int((~cancelled).sum())
    want = max(round(TRANSFERRED_SHARE * n_disbursed) - int(forced.sum()), 0)
    chosen = rng.choice(candidates, size=min(want, len(candidates)), replace=False)
    transfer = forced.copy()
    transfer[chosen] = True
    active = np.flatnonzero(branches.is_active)
    p = branches.weights[active] / branches.weights[active].sum()
    for i in np.flatnonzero(transfer):
        while True:
            target = int(rng.choice(active, p=p))
            if target != booking[i]:
                servicing[i] = target
                break
    return servicing


def _simulate_contract(
    rng: np.random.Generator,
    out: _Out,
    *,
    contract_id: int,
    servicing_index: int,
    principal: int,
    rate_h: int,
    disb_day: int,
    year: int,
    month: int,
    dom: int,
    dues: IntArray,
    behaviour: int,
    restructure: tuple[int, int] | None,
) -> tuple[str, int | None, int, int]:
    """Build one contract's schedule, payments, snapshots and collection actions.

    Returns (status, closed day, maturity day, first due day).
    """
    tenor = len(dues)
    prin = _split(principal, tenor)
    interest_total = principal * rate_h * tenor // 120_000
    intr = _split(interest_total, tenor)

    # Schedule rows: version 1 first, then version 2 after a restructuring.
    version = [1] * tenor
    eff_from = [disb_day] * tenor
    sup: list[int] = [INF] * tenor
    due_all = [int(x) for x in dues]
    prin_all = list(prin)
    intr_all = list(intr)
    payable = list(range(tenor))
    restructured_at = None
    if restructure is not None:
        j, restructured_at = restructure
        remaining = sum(prin[j:])
        extra = int(rng.integers(3, 13))
        count2 = (tenor - j) + extra
        interest2 = remaining * rate_h * count2 // 120_000
        prin2 = _split(remaining, count2)
        intr2 = _split(interest2, count2)
        for k in range(j, tenor):
            sup[k] = restructured_at
        payable = list(range(j))
        for i in range(count2):
            payable.append(len(due_all))
            due_all.append(_due_day(year, month, dom, j + 1 + i))
            prin_all.append(prin2[i])
            intr_all.append(intr2[i])
            version.append(2)
            eff_from.append(restructured_at)
            sup.append(INF)

    n_rows = len(due_all)
    base = out.n_schedule_rows
    seq_no = [k + 1 for k in range(tenor)] + [i + 1 for i in range(n_rows - tenor)]
    for r in range(n_rows):
        out.sched["contract_id"].append(contract_id)
        out.sched["installment_no"].append(seq_no[r])
        out.sched["schedule_version"].append(version[r])
        out.sched["is_current"].append(sup[r] == INF)
        out.sched["effective_from"].append(eff_from[r])
        out.sched["superseded_date"].append(None if sup[r] == INF else sup[r])
        out.sched["due_date"].append(due_all[r])
        out.sched["principal_due"].append(prin_all[r])
        out.sched["interest_due"].append(intr_all[r])
        out.sched["installment_amount"].append(prin_all[r] + intr_all[r])
    out.n_schedule_rows += n_rows

    # Payments against the rows still payable; only rows already due can have been paid.
    pay_rows = np.array([r for r in payable if due_all[r] <= ref.ASOF_DAY], dtype=np.int64)
    m = len(pay_rows)
    post_eff = np.full(n_rows, INF, dtype=np.int64)  # posting day of the effective payment
    if m:
        due = np.array(due_all, dtype=np.int64)[pay_rows]
        inst = (
            np.array(prin_all, dtype=np.int64)[pay_rows]
            + np.array(intr_all, dtype=np.int64)[pay_rows]
        )
        is_v2 = np.array(version, dtype=np.int64)[pay_rows] == 2
        seq = np.arange(1, m + 1)
        on_time = rng.integers(0, GRACE_DAYS + 1, size=m)
        late = rng.integers(GRACE_DAYS + 1, 46, size=m)
        very_late = rng.integers(60, 186, size=m)
        paid = np.ones(m, dtype=np.bool_)
        if behaviour == ON_TIME:
            offset = on_time
        elif behaviour == LATE:
            offset = late
        elif behaviour == VERY_LATE:
            start = int(rng.integers(1, m + 1))
            length = int(rng.integers(3, 7))
            window = (seq >= start) & (seq < start + length) & ~is_v2
            offset = np.where(window, very_late, on_time)
        else:
            offset = on_time
            paid = seq <= int(rng.integers(1, max(2, m)))
        pay_day = due + offset
        post = pay_day + rng.integers(0, 4, size=m)
        valid = paid & (post <= ref.ASOF_DAY)
        penalty = np.where(offset > GRACE_DAYS, inst * offset // 1000, 0)
        channel = rng.choice(len(ref.PAYMENT_CHANNELS), size=m, p=_CHANNEL_P)
        reversed_ = valid & (pay_day <= ref.ASOF_DAY - 12) & (rng.random(m) < REVERSAL_PROBABILITY)
        replay_pay = pay_day + rng.integers(1, 9, size=m)
        replay_post = replay_pay + rng.integers(0, 4, size=m)
        replay_penalty = np.where(
            replay_pay - due > GRACE_DAYS, inst * (replay_pay - due) // 1000, 0
        )
        replay_channel = rng.choice(len(ref.PAYMENT_CHANNELS), size=m, p=_CHANNEL_P)
        for i in range(m):
            if not valid[i]:
                continue
            r = int(pay_rows[i])
            _add_payment(
                out, contract_id, base + r + 1, int(pay_day[i]), int(post[i]),
                prin_all[r], intr_all[r], int(penalty[i]), int(channel[i]), bool(reversed_[i]),
            )  # fmt: skip
            if reversed_[i]:
                _add_payment(
                    out, contract_id, base + r + 1, int(replay_pay[i]), int(replay_post[i]),
                    prin_all[r], intr_all[r], int(replay_penalty[i]), int(replay_channel[i]), False,
                )  # fmt: skip
                post_eff[r] = replay_post[i]
            else:
                post_eff[r] = post[i]

    # Closing: fully repaid when the principal of every payable row has been received.
    paid_principal = sum(prin_all[r] for r in payable if post_eff[r] < INF)
    closed_day = None
    if paid_principal == principal:
        closed_day = int(max(post_eff[r] for r in payable))

    # Month-end snapshots (docs/SAMPLE_DOMAIN.md, "How the snapshot table is derived").
    first_e = int(np.searchsorted(MONTH_ENDS, disb_day, side="left"))
    ends = MONTH_ENDS[first_e:]
    posts = np.array(sorted((int(post_eff[r]), prin_all[r]) for r in payable if post_eff[r] < INF))
    if len(posts):
        cum = np.concatenate([[0], np.cumsum(posts[:, 1])])
        received = cum[np.searchsorted(posts[:, 0], ends, side="right")]
    else:
        received = np.zeros(len(ends), dtype=np.int64)
    outstanding = principal - received
    due_a = np.array(due_all, dtype=np.int64)[None, :]
    e_col = ends[:, None]
    in_arrears = (
        (due_a < e_col)
        & (np.array(eff_from, dtype=np.int64)[None, :] <= e_col)
        & (np.array(sup, dtype=np.int64)[None, :] > e_col)
        & (post_eff[None, :] > e_col)
    )
    oldest = np.where(in_arrears, due_a, INF).min(axis=1)
    dpd = np.where(oldest < INF, ends - oldest, 0)
    gone = np.flatnonzero(outstanding <= 0)
    stop_paid = int(gone[0]) if len(gone) else len(ends)
    bad = np.flatnonzero(dpd > WRITE_OFF_DPD)
    stop_bad = int(bad[0]) if len(bad) else len(ends)
    status = "active"
    if stop_bad < stop_paid:
        last = stop_bad + 1
        status = "written_off"
        closed_day = int(ends[stop_bad])
    else:
        last = stop_paid
        if closed_day is not None:
            status = "closed"
    for i in range(last):
        out.snap["snapshot_date"].append(int(ends[i]))
        out.snap["contract_id"].append(contract_id)
        out.snap["outstanding_principal"].append(int(outstanding[i]))
        out.snap["days_past_due"].append(int(dpd[i]))

    # Collection actions on installments that were paid late or are still unpaid.
    if m:
        wo_day = closed_day if status == "written_off" else None
        paid_day = post_eff[pay_rows]
        end = np.where(paid_day < INF, paid_day, ref.ASOF_DAY)
        if wo_day is not None:
            end = np.minimum(end, wo_day)
        over = end - due
        pick = (over > GRACE_DAYS) & (rng.random(m) < ACTION_PROBABILITY)
        if pick.any():
            counts = np.minimum(MAX_ACTIONS_PER_INSTALLMENT, 1 + over[pick] // 20)
            which = np.repeat(np.flatnonzero(pick), counts)
            k = len(which)
            offset_days = rng.integers(GRACE_DAYS + 1, over[which] + 1)
            bin_ = np.searchsorted([7, 30, 60], offset_days, side="left")
            type_idx = (rng.random(k)[:, None] >= _TYPE_CUM[bin_]).sum(axis=1)
            out_idx = (rng.random(k)[:, None] >= _OUTCOME_CUM[type_idx]).sum(axis=1)
            promise = out_idx == 0
            promise_day = due[which] + offset_days + rng.integers(2, 15, size=k)
            share = np.where(rng.random(k) < 0.8, 100, 50)
            collector = 1000 + servicing_index * 8 + rng.integers(0, 8, size=k)
            for t in range(k):
                out.act["kontrak_id"].append(contract_id)
                out.act["tgl_aksi"].append(int(due[which[t]] + offset_days[t]))
                out.act["jns_aksi"].append(int(type_idx[t]))
                out.act["kolektor_id"].append(int(collector[t]))
                out.act["hasil"].append(int(out_idx[t]))
                out.act["tgl_janji"].append(int(promise_day[t]) if promise[t] else None)
                out.act["nom_janji"].append(
                    int(inst[which[t]] * share[t] // 100) if promise[t] else None
                )

    return status, closed_day, max(due_all[r] for r in payable), int(dues[0])


def _add_payment(
    out: _Out,
    contract_id: int,
    schedule_id: int,
    pay_day: int,
    post_day: int,
    principal: int,
    interest: int,
    penalty: int,
    channel: int,
    reversed_: bool,
) -> None:
    out.pay["contract_id"].append(contract_id)
    out.pay["schedule_id"].append(schedule_id)
    out.pay["payment_date"].append(pay_day)
    out.pay["posting_date"].append(post_day)
    out.pay["principal_paid"].append(principal)
    out.pay["interest_paid"].append(interest)
    out.pay["penalty_paid"].append(penalty)
    out.pay["payment_channel"].append(channel)
    out.pay["is_reversed"].append(reversed_)


def _schedule_columns(out: _Out) -> Columns:
    s = out.sched
    n = len(s["contract_id"])
    cols: Columns = {"schedule_id": list(range(1, n + 1))}
    for key, values in s.items():
        cols[key] = list(values)
    return cols


def _payment_columns(out: _Out) -> Columns:
    p = out.pay
    n = len(p["contract_id"])
    order = np.lexsort(
        (
            np.array([not r for r in p["is_reversed"]]),
            np.array(p["schedule_id"]),
            np.array(p["contract_id"]),
            np.array(p["payment_date"]),
            np.array(p["posting_date"]),
        )
    )
    take = [int(i) for i in order]

    def col(name: str) -> list[Any]:
        return [p[name][i] for i in take]

    principal, interest, penalty = col("principal_paid"), col("interest_paid"), col("penalty_paid")
    channels = col("payment_channel")
    cols: Columns = {
        "payment_id": list(range(1, n + 1)),
        "contract_id": col("contract_id"),
        "schedule_id": col("schedule_id"),
        "payment_date": col("payment_date"),
        "posting_date": col("posting_date"),
        "amount_paid": [
            int(a) + int(b) + int(c) for a, b, c in zip(principal, interest, penalty, strict=True)
        ],
        "principal_paid": principal,
        "interest_paid": interest,
        "penalty_paid": penalty,
        "payment_channel": [ref.PAYMENT_CHANNELS[int(c)] for c in channels],
        "is_reversed": col("is_reversed"),
    }
    return cols


def _action_columns(out: _Out) -> Columns:
    a = out.act
    n = len(a["kontrak_id"])
    order = np.lexsort((np.array(a["kontrak_id"]), np.array(a["tgl_aksi"])))
    take = [int(i) for i in order]

    def col(name: str) -> list[Any]:
        return [a[name][i] for i in take]

    return {
        "act_id": list(range(1, n + 1)),
        "kontrak_id": col("kontrak_id"),
        "tgl_aksi": col("tgl_aksi"),
        "jns_aksi": [ACTION_TYPES[int(t)] for t in col("jns_aksi")],
        "kolektor_id": col("kolektor_id"),
        "hasil": [ACTION_OUTCOMES[int(t)] for t in col("hasil")],
        "tgl_janji": col("tgl_janji"),
        "nom_janji": col("nom_janji"),
    }


_DPD_BUCKETS = (
    (0, "current"),
    (30, "dpd_1_30"),
    (60, "dpd_31_60"),
    (90, "dpd_61_90"),
    (180, "dpd_91_180"),
)


def dpd_bucket(days: int) -> str:
    for upper, name in _DPD_BUCKETS:
        if days <= upper:
            return name
    return "dpd_over_180"


def collectibility(days: int) -> str:
    for upper, name in ((10, "1"), (90, "2"), (120, "3"), (180, "4")):
        if days <= upper:
            return name
    return "5"


def _snapshot_columns(out: _Out) -> Columns:
    s = out.snap
    order = np.lexsort((np.array(s["contract_id"]), np.array(s["snapshot_date"])))
    take = [int(i) for i in order]
    dpd = [s["days_past_due"][i] for i in take]
    return {
        "snapshot_date": [s["snapshot_date"][i] for i in take],
        "contract_id": [s["contract_id"][i] for i in take],
        "outstanding_principal": [s["outstanding_principal"][i] for i in take],
        "days_past_due": dpd,
        "dpd_bucket": [dpd_bucket(d) for d in dpd],
        "collectibility": [collectibility(d) for d in dpd],
    }
