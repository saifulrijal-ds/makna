"""Table specification of the synthetic lending data (docs/SAMPLE_DOMAIN.md sections 3 and 6.1).

This module is the single description of the nine sample tables: names, columns, SQL types,
the true meaning of every object, the catalog description (which is deliberately poor in places,
D-14), personal-data flags, keys and relationships. The generator, the development stack loader
and the catalog export all read it. It imports nothing outside the standard library.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

SQL_TYPES = (
    "varchar",
    "integer",
    "decimal(18,2)",
    "decimal(5,2)",
    "date",
    "timestamp",
    "boolean",
)


@dataclass(frozen=True)
class ColumnSpec:
    """One column of a sample table."""

    name: str
    sql_type: str
    meaning: str
    """True meaning (docs/SAMPLE_DOMAIN.md section 3). Not what the catalog says."""
    catalog_description: str
    """What the catalog export says. Empty string means the catalog has no description."""
    is_personal: bool = False
    """True for the five personal columns of `customers` (tag `PII.Sensitive`)."""
    semantic_name: str | None = None
    """Clean name the semantic model gives a legacy column, when it differs from `name`."""


@dataclass(frozen=True)
class ForeignKey:
    """A many-to-one relationship (section 3.10)."""

    column: str
    ref_table: str
    ref_column: str
    relationship: str
    """Relationship name in the semantic model, for example `contract.booking_branch`."""


@dataclass(frozen=True)
class TableSpec:
    """One sample table."""

    name: str
    entity: str
    """Entity name in the semantic model."""
    meaning: str
    catalog_description: str
    """Empty string means the catalog has no description."""
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKey, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> ColumnSpec:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"{self.name} has no column {name!r}")

    @property
    def personal_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.is_personal)


# Catalog descriptions that are deliberately not the plain English meaning (section 6.1).
# Key: (table, column) or (table, None) for the table itself. Value "" means "none (empty)".
CATALOG_EXCEPTIONS: Mapping[tuple[str, str | None], str] = {
    ("loan_applications", None): "LOS extract",
    ("installment_schedules", None): "",
    ("collection_actions", None): "",
    ("contract_monthly_snapshots", None): "Data bulanan kontrak",
    **{
        ("loan_applications", c): ""
        for c in ("saluran", "req_amt", "appr_amt", "req_tenor", "grade_risiko")
    },
    ("loan_applications", "sts_putusan"): "Status",
    ("loan_applications", "tgl_pengajuan"): "Tanggal",
    ("loan_applications", "tgl_putusan"): "Tanggal",
    **{
        ("installment_schedules", c): ""
        for c in ("schedule_version", "is_current", "superseded_date", "effective_from")
    },
    ("payments", "posting_date"): "Tgl posting",
    ("payments", "is_reversed"): "Flag",
    ("contracts", "servicing_branch_id"): "Cabang",
    ("contracts", "disbursement_date"): "Tanggal pencairan dana",
    ("contract_monthly_snapshots", "collectibility"): "Kol",
    ("contract_monthly_snapshots", "days_past_due"): "DPD",
    ("customers", "income_band"): "Range penghasilan per bulan",
}

# Every column of collection_actions has no catalog description.
_ALL_COLUMNS_EMPTY = frozenset({"collection_actions"})


def _c(
    name: str,
    sql_type: str,
    meaning: str,
    description: str,
    *,
    personal: bool = False,
    semantic: str | None = None,
) -> tuple[str, str, str, str, bool, str | None]:
    return (name, sql_type, meaning, description, personal, semantic)


def _table(
    name: str,
    entity: str,
    meaning: str,
    description: str,
    columns: list[tuple[str, str, str, str, bool, str | None]],
    primary_key: tuple[str, ...],
    foreign_keys: tuple[ForeignKey, ...] = (),
) -> TableSpec:
    table_description = CATALOG_EXCEPTIONS.get((name, None), description)
    specs = []
    for col_name, sql_type, col_meaning, col_description, personal, semantic in columns:
        if name in _ALL_COLUMNS_EMPTY:
            col_description = ""
        else:
            col_description = CATALOG_EXCEPTIONS.get((name, col_name), col_description)
        specs.append(
            ColumnSpec(
                name=col_name,
                sql_type=sql_type,
                meaning=col_meaning,
                catalog_description=col_description,
                is_personal=personal,
                semantic_name=semantic,
            )
        )
    return TableSpec(
        name, entity, meaning, table_description, tuple(specs), primary_key, foreign_keys
    )


_BRANCHES = _table(
    "branches",
    "branch",
    "One row per branch.",
    "Branch offices, one row per branch.",
    [
        _c("branch_id", "integer", "Primary key.", "Branch identifier."),
        _c("branch_code", "varchar", "Short code, for example BDG01.", "Short branch code."),
        _c("branch_name", "varchar", "Name of the branch.", "Name of the branch."),
        _c(
            "area_name",
            "varchar",
            "Area the branch belongs to (about 12 areas).",
            "Area the branch belongs to.",
        ),
        _c(
            "region_name",
            "varchar",
            "Region the area belongs to (5 regions: Jawa Barat, Jawa Tengah, Jawa Timur, "
            "Sumatera, Kalimantan Sulawesi).",
            "Sales region the area belongs to.",
        ),
        _c(
            "province",
            "varchar",
            "Province of the office address. Not the same as region: one region spans several "
            "provinces.",
            "Province of the office address.",
        ),
        _c("opened_date", "date", "Date the branch opened.", "Date the branch opened."),
        _c(
            "is_active",
            "boolean",
            "False for 2 closed branches, which still have old contracts.",
            "False for closed branches.",
        ),
    ],
    ("branch_id",),
)

_PRODUCTS = _table(
    "products",
    "product",
    "One row per product.",
    "Loan products, one row per product.",
    [
        _c("product_id", "integer", "Primary key.", "Product identifier."),
        _c("product_code", "varchar", "Short code.", "Short product code."),
        _c("product_name", "varchar", "Commercial name.", "Commercial name of the product."),
        _c(
            "product_category",
            "varchar",
            "car_backed, motorcycle_backed, used_car_purchase, unsecured.",
            "Product category.",
        ),
        _c("financing_scheme", "varchar", "conventional or sharia.", "Conventional or sharia."),
    ],
    ("product_id",),
)

_CUSTOMERS = _table(
    "customers",
    "customer",
    "One row per customer.",
    "Customers, one row per customer.",
    [
        _c("customer_id", "integer", "Primary key.", "Customer identifier."),
        _c("full_name", "varchar", "Generated name.", "Full name of the customer.", personal=True),
        _c(
            "national_id",
            "varchar",
            "16 digits, always starting with 99 so it can never be a real identity number.",
            "National identity number of the customer.",
            personal=True,
        ),
        _c(
            "phone_number",
            "varchar",
            "Always starts with 0899000 (not an assigned range in this dataset's fiction).",
            "Mobile phone number of the customer.",
            personal=True,
        ),
        _c(
            "email",
            "varchar",
            "Always at example.com.",
            "Email address of the customer.",
            personal=True,
        ),
        _c("birth_date", "date", "Date of birth.", "Date of birth of the customer.", personal=True),
        _c("gender", "varchar", "F or M.", "Gender of the customer."),
        _c(
            "occupation_group",
            "varchar",
            "employee, self_employed, civil_servant, professional, other.",
            "Occupation group of the customer.",
        ),
        _c(
            "income_band",
            "varchar",
            "lt_5m, 5m_10m, 10m_20m, gt_20m (monthly income in rupiah).",
            "Monthly income band of the customer.",
        ),
        _c("city", "varchar", "City of the home address.", "City of the home address."),
        _c("province", "varchar", "Province of the home address.", "Province of the home address."),
        _c(
            "customer_segment",
            "varchar",
            "mass, emerging_affluent, affluent.",
            "Customer segment.",
        ),
        _c(
            "home_branch_id",
            "integer",
            "Branch where the customer first registered. References branches.",
            "Branch where the customer first registered.",
        ),
        _c(
            "registered_at",
            "timestamp",
            "When the customer record was created.",
            "When the customer record was created.",
        ),
    ],
    ("customer_id",),
    (ForeignKey("home_branch_id", "branches", "branch_id", "customer.home_branch"),),
)

_LOAN_APPLICATIONS = _table(
    "loan_applications",
    "application",
    "One row per application. Legacy abbreviated, mixed-language column names (Q11).",
    "Loan applications, one row per application.",
    [
        _c(
            "appl_id",
            "integer",
            "Primary key.",
            "Application identifier.",
            semantic="application_id",
        ),
        _c(
            "cust_id",
            "integer",
            "References customers.",
            "Customer who applied.",
            semantic="customer_id",
        ),
        _c(
            "prod_id",
            "integer",
            "References products.",
            "Product applied for.",
            semantic="product_id",
        ),
        _c(
            "cab_id",
            "integer",
            "Branch (cabang) that received the application. References branches.",
            "Branch that received the application.",
            semantic="branch_id",
        ),
        _c(
            "saluran",
            "varchar",
            "Channel (saluran): walk_in, agent, online, telesales.",
            "Channel of the application.",
            semantic="channel",
        ),
        _c(
            "tgl_pengajuan",
            "timestamp",
            "When the application was submitted (tanggal pengajuan).",
            "When the application was submitted.",
            semantic="submitted_at",
        ),
        _c(
            "tgl_putusan",
            "date",
            "Date of the credit decision (tanggal putusan). Null while in process.",
            "Date of the credit decision.",
            semantic="decision_date",
        ),
        _c(
            "sts_putusan",
            "varchar",
            "Decision status: approved, rejected, cancelled (customer withdrew before a "
            "decision), in_process.",
            "Decision status of the application.",
            semantic="decision_status",
        ),
        _c(
            "req_amt",
            "decimal(18,2)",
            "Amount the customer asked for.",
            "Amount requested.",
            semantic="requested_amount",
        ),
        _c(
            "appr_amt",
            "decimal(18,2)",
            "Amount approved. Null unless approved. Often lower than requested.",
            "Amount approved.",
            semantic="approved_amount",
        ),
        _c(
            "req_tenor",
            "integer",
            "Tenor in months the customer asked for.",
            "Tenor in months requested.",
            semantic="requested_tenor_months",
        ),
        _c(
            "grade_risiko",
            "varchar",
            "Risk grade A to E, assigned at decision. Null while in process.",
            "Risk grade assigned at decision.",
            semantic="risk_grade",
        ),
    ],
    ("appl_id",),
    (
        ForeignKey("cust_id", "customers", "customer_id", "application.customer"),
        ForeignKey("prod_id", "products", "product_id", "application.product"),
        ForeignKey("cab_id", "branches", "branch_id", "application.branch"),
    ),
)

_CONTRACTS = _table(
    "contracts",
    "contract",
    "One row per contract.",
    "Loan contracts, one row per contract.",
    [
        _c("contract_id", "integer", "Primary key.", "Contract identifier."),
        _c(
            "application_id",
            "integer",
            "References loan_applications. One approved application leads to at most one contract.",
            "Application the contract came from.",
        ),
        _c("customer_id", "integer", "References customers.", "Customer who signed the contract."),
        _c("product_id", "integer", "References products.", "Product of the contract."),
        _c(
            "booking_branch_id",
            "integer",
            "Branch that originated the contract. Never changes. References branches.",
            "Branch that originated the contract.",
        ),
        _c(
            "servicing_branch_id",
            "integer",
            "Branch that services and collects the contract now. References branches.",
            "Branch that services the contract now.",
        ),
        _c("signed_date", "date", "Date the customer signed.", "Date the customer signed."),
        _c(
            "disbursement_date",
            "date",
            "Date the money was released, 0 to 5 days after signing. Null for cancelled contracts.",
            "Date the money was released.",
        ),
        _c(
            "first_due_date",
            "date",
            "Due date of the first installment. Null for cancelled contracts.",
            "Due date of the first installment.",
        ),
        _c(
            "maturity_date",
            "date",
            "Due date of the last installment under the current schedule. Null for cancelled "
            "contracts.",
            "Due date of the last installment.",
        ),
        _c(
            "closed_date",
            "date",
            "Date the contract was fully repaid or written off. Null while active.",
            "Date the contract was closed.",
        ),
        _c("principal_amount", "decimal(18,2)", "Principal financed.", "Principal financed."),
        _c(
            "tenor_months",
            "integer",
            "Number of installments at signing (12, 18, 24, 36, or 48).",
            "Number of installments at signing.",
        ),
        _c(
            "annual_rate_pct",
            "decimal(5,2)",
            "Flat annual rate in percent.",
            "Flat annual rate in percent.",
        ),
        _c(
            "contract_status",
            "varchar",
            "active, closed, written_off, cancelled.",
            "Status of the contract.",
        ),
        _c(
            "is_restructured",
            "boolean",
            "True if the schedule was restructured at least once.",
            "True if the contract was restructured.",
        ),
        _c(
            "restructured_date",
            "date",
            "Date of the restructuring. Null if never restructured.",
            "Date of the restructuring.",
        ),
    ],
    ("contract_id",),
    (
        ForeignKey("application_id", "loan_applications", "appl_id", "contract.application"),
        ForeignKey("customer_id", "customers", "customer_id", "contract.customer"),
        ForeignKey("product_id", "products", "product_id", "contract.product"),
        ForeignKey("booking_branch_id", "branches", "branch_id", "contract.booking_branch"),
        ForeignKey("servicing_branch_id", "branches", "branch_id", "contract.servicing_branch"),
    ),
)

_INSTALLMENT_SCHEDULES = _table(
    "installment_schedules",
    "installment",
    "One row per scheduled installment, including replaced ones.",
    "Installment schedule rows, one per scheduled installment.",
    [
        _c("schedule_id", "integer", "Primary key.", "Schedule row identifier."),
        _c(
            "contract_id",
            "integer",
            "References contracts.",
            "Contract the installment belongs to.",
        ),
        _c(
            "installment_no",
            "integer",
            "Sequence number within its schedule version.",
            "Installment number within the schedule version.",
        ),
        _c(
            "schedule_version",
            "integer",
            "1 for the original schedule, 2 after a restructuring.",
            "Schedule version.",
        ),
        _c(
            "is_current",
            "boolean",
            "False for rows replaced by a restructuring.",
            "False for replaced rows.",
        ),
        _c(
            "effective_from",
            "date",
            "Date this row became part of the schedule (disbursement date or restructuring date).",
            "Date the row became part of the schedule.",
        ),
        _c(
            "superseded_date",
            "date",
            "Date this row was replaced. Null for current rows.",
            "Date the row was replaced.",
        ),
        _c(
            "due_date", "date", "Date the installment falls due.", "Date the installment falls due."
        ),
        _c("principal_due", "decimal(18,2)", "Principal part.", "Principal part."),
        _c("interest_due", "decimal(18,2)", "Interest part.", "Interest part."),
        _c(
            "installment_amount",
            "decimal(18,2)",
            "principal_due + interest_due.",
            "Principal plus interest.",
        ),
    ],
    ("schedule_id",),
    (ForeignKey("contract_id", "contracts", "contract_id", "installment.contract"),),
)

_PAYMENTS = _table(
    "payments",
    "payment",
    "One row per payment received.",
    "Payments received, one row per payment.",
    [
        _c("payment_id", "integer", "Primary key.", "Payment identifier."),
        _c("contract_id", "integer", "References contracts.", "Contract the payment is for."),
        _c(
            "schedule_id",
            "integer",
            "The installment this payment was applied to. References installment_schedules.",
            "Installment the payment was applied to.",
        ),
        _c(
            "payment_date",
            "date",
            "Date the customer paid, as reported by the channel.",
            "Date the customer paid.",
        ),
        _c(
            "posting_date",
            "date",
            "Date the payment was booked in the core system, 0 to 3 days after payment_date.",
            "Date the payment was booked.",
        ),
        _c(
            "amount_paid",
            "decimal(18,2)",
            "principal_paid + interest_paid + penalty_paid.",
            "Total amount paid.",
        ),
        _c(
            "principal_paid",
            "decimal(18,2)",
            "Part applied to principal.",
            "Part applied to principal.",
        ),
        _c(
            "interest_paid",
            "decimal(18,2)",
            "Part applied to interest.",
            "Part applied to interest.",
        ),
        _c(
            "penalty_paid",
            "decimal(18,2)",
            "Late payment penalty collected. Zero when paid on time.",
            "Late payment penalty collected.",
        ),
        _c(
            "payment_channel",
            "varchar",
            "bank_transfer, virtual_account, retail_outlet, branch_cashier, collector.",
            "Channel the payment came through.",
        ),
        _c(
            "is_reversed",
            "boolean",
            "True for a payment that was later reversed (for example a failed transfer). The "
            "row stays.",
            "True if the payment was reversed.",
        ),
    ],
    ("payment_id",),
    (
        ForeignKey("contract_id", "contracts", "contract_id", "payment.contract"),
        ForeignKey("schedule_id", "installment_schedules", "schedule_id", "payment.installment"),
    ),
)

_COLLECTION_ACTIONS = _table(
    "collection_actions",
    "collection_action",
    "One row per collection activity. Legacy abbreviated, mixed-language column names (Q11).",
    "Collection activities, one row per activity.",
    [
        _c("act_id", "integer", "Primary key.", "", semantic="action_id"),
        _c("kontrak_id", "integer", "References contracts.", "", semantic="contract_id"),
        _c(
            "tgl_aksi",
            "date",
            "Date of the activity (tanggal aksi).",
            "",
            semantic="action_date",
        ),
        _c(
            "jns_aksi",
            "varchar",
            "Type of activity (jenis aksi): reminder_message, phone_call, field_visit, "
            "warning_letter.",
            "",
            semantic="action_type",
        ),
        _c(
            "kolektor_id",
            "integer",
            "Staff identifier (a number only; no staff table).",
            "",
            semantic="collector_id",
        ),
        _c(
            "hasil",
            "varchar",
            "Outcome (hasil): promise_to_pay, paid, no_contact, refused.",
            "",
            semantic="outcome",
        ),
        _c(
            "tgl_janji",
            "date",
            "Date the customer promised to pay (tanggal janji). Null unless the outcome is "
            "promise_to_pay.",
            "",
            semantic="promise_date",
        ),
        _c(
            "nom_janji",
            "decimal(18,2)",
            "Amount promised (nominal janji). Null unless the outcome is promise_to_pay.",
            "",
            semantic="promise_amount",
        ),
    ],
    ("act_id",),
    (ForeignKey("kontrak_id", "contracts", "contract_id", "collection_action.contract"),),
)

_CONTRACT_MONTHLY_SNAPSHOTS = _table(
    "contract_monthly_snapshots",
    "contract_snapshot",
    "One row per contract per month end, derived from the other tables.",
    "Month-end position of each contract.",
    [
        _c(
            "snapshot_date",
            "date",
            "A month-end date. Part of the primary key.",
            "Month-end date of the position.",
        ),
        _c(
            "contract_id",
            "integer",
            "References contracts. Part of the primary key.",
            "Contract the position is for.",
        ),
        _c(
            "outstanding_principal",
            "decimal(18,2)",
            "Principal still owed at the snapshot date. Always greater than zero (contracts "
            "with nothing owed have no row).",
            "Principal still owed at the snapshot date.",
        ),
        _c(
            "days_past_due",
            "integer",
            "Days since the oldest unpaid installment fell due; 0 if none is overdue.",
            "Days since the oldest unpaid installment fell due.",
        ),
        _c(
            "dpd_bucket",
            "varchar",
            "current (0), dpd_1_30, dpd_31_60, dpd_61_90, dpd_91_180, dpd_over_180.",
            "Past-due bucket.",
        ),
        _c(
            "collectibility",
            "varchar",
            "1 (0 to 10 days), 2 (11 to 90), 3 (91 to 120), 4 (121 to 180), 5 (over 180).",
            "Collectibility class from 1 to 5.",
        ),
    ],
    ("snapshot_date", "contract_id"),
    (ForeignKey("contract_id", "contracts", "contract_id", "contract_snapshot.contract"),),
)

TABLES: tuple[TableSpec, ...] = (
    _BRANCHES,
    _PRODUCTS,
    _CUSTOMERS,
    _LOAN_APPLICATIONS,
    _CONTRACTS,
    _INSTALLMENT_SCHEDULES,
    _PAYMENTS,
    _COLLECTION_ACTIONS,
    _CONTRACT_MONTHLY_SNAPSHOTS,
)

TABLES_BY_NAME: Mapping[str, TableSpec] = {t.name: t for t in TABLES}


def get_table(name: str) -> TableSpec:
    """Return the specification of one table by name."""
    try:
        return TABLES_BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown sample table {name!r}") from None
