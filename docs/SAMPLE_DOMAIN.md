# Sample Domain: Synthetic Consumer Lending

Status: design output (2026-11-11). This document specifies the synthetic data (DAT-1, DAT-2) and the sample metrics (DAT-3). The data generator (WP-02) and the sample definitions (WP-04) are built from it. Where this document and a YAML file in `semantic/schema/examples/` disagree, say so in your report; the YAML examples were validated and compiled in a spike, so they are the more likely to be right.

Everything here is invented. The company, its branches, and its customers do not exist. The definitions follow common practice in consumer financing so that the platform is tested on realistic concepts; they are not regulatory guidance.

## 1. The business in one paragraph

A financing company lends to individuals through branch offices. A customer submits an **application** for a **product**. If it is approved, the customer signs a **contract** and the money is **disbursed** a few days later. The contract has a monthly **installment schedule**. Customers make **payments**; some pay late, some stop paying. Staff run **collection actions** on late contracts. Some contracts are **restructured** (the remaining schedule is replaced by an easier one). At each month end the company records every contract's **position**: how much principal is still owed and how many days it is past due.

## 2. Physical layout

- Target engine: PostgreSQL, database `lending`, schema `core` (D-22). A table is addressed as `lending.core.<table>`; unit tests attach a DuckDB database named `lending` so the same names resolve there.
- In the development stack the tables live in that PostgreSQL database (see `docs/ARCHITECTURE.md` section 9).
- Clock: the data covers **2024-01-01 to 2026-06-30**. The "as of" date of the dataset is 2026-06-30. Evals use a fixed "today" of 2026-07-01 (see `docs/EVALS.md`).
- Random seed: `20261111`. The same seed must produce byte-identical output files.
- Target sizes (approximate; the generator takes a `--scale` factor, default 1.0): 40 branches, 8 products, 6,000 customers, 9,000 applications, 5,500 contracts, 130,000 schedule rows, 90,000 payments, 80,000 snapshot rows, 15,000 collection actions.

## 3. Tables and columns

Types are SQL types with the same names in PostgreSQL and DuckDB (`varchar`, `integer`, `decimal(18,2)`, `date`, `timestamp`, `boolean`). "PII" marks personal data. The catalog export (WP-06) carries the tag `PII.Sensitive` on these columns.

The "Meaning" column is the true meaning, for the generator and for people writing definitions. It is **not** what the catalog says: the catalog's descriptions are specified separately in section 6, and some are deliberately missing or vague (D-14). Two tables, `loan_applications` and `collection_actions`, come from older source systems and have abbreviated column names that mix Indonesian and English (quirk Q11). The semantic model gives them clean names: for example the dimension `application.decision_status` reads the column `sts_putusan`.

### 3.1 `branches` (one row per branch)

| Column | Type | Meaning |
|---|---|---|
| `branch_id` | integer | Primary key. |
| `branch_code` | varchar | Short code, for example `BDG01`. |
| `branch_name` | varchar | Name of the branch. |
| `area_name` | varchar | Area the branch belongs to (about 12 areas). |
| `region_name` | varchar | Region the area belongs to (5 regions: `Jawa Barat`, `Jawa Tengah`, `Jawa Timur`, `Sumatera`, `Kalimantan Sulawesi`). |
| `province` | varchar | Province of the office address. Not the same as region: one region spans several provinces. |
| `opened_date` | date | Date the branch opened. |
| `is_active` | boolean | False for 2 closed branches, which still have old contracts. |

### 3.2 `products` (one row per product)

| Column | Type | Meaning |
|---|---|---|
| `product_id` | integer | Primary key. |
| `product_code` | varchar | Short code. |
| `product_name` | varchar | Commercial name. |
| `product_category` | varchar | `car_backed`, `motorcycle_backed`, `used_car_purchase`, `unsecured`. |
| `financing_scheme` | varchar | `conventional` or `sharia`. |

### 3.3 `customers` (one row per customer)

| Column | Type | Meaning |
|---|---|---|
| `customer_id` | integer | Primary key. |
| `full_name` | varchar | PII. Generated name. |
| `national_id` | varchar | PII. 16 digits, always starting with `99` so it can never be a real identity number. |
| `phone_number` | varchar | PII. Always starts with `0899000` (not an assigned range in this dataset's fiction). |
| `email` | varchar | PII. Always at `example.com`. |
| `birth_date` | date | PII. |
| `gender` | varchar | `F` or `M`. |
| `occupation_group` | varchar | `employee`, `self_employed`, `civil_servant`, `professional`, `other`. |
| `income_band` | varchar | `lt_5m`, `5m_10m`, `10m_20m`, `gt_20m` (monthly income in rupiah). |
| `city` | varchar | City of the home address. |
| `province` | varchar | Province of the home address. |
| `customer_segment` | varchar | `mass`, `emerging_affluent`, `affluent`. |
| `home_branch_id` | integer | Branch where the customer first registered. References `branches`. |
| `registered_at` | timestamp | When the customer record was created. |

### 3.4 `loan_applications` (one row per application)

Abbreviated, mixed-language column names (Q11). The semantic name each column should get is in brackets.

| Column | Type | Meaning |
|---|---|---|
| `appl_id` | integer | Primary key. [`application_id`] |
| `cust_id` | integer | References `customers`. [`customer_id`] |
| `prod_id` | integer | References `products`. [`product_id`] |
| `cab_id` | integer | Branch (*cabang*) that received the application. References `branches`. [`branch_id`] |
| `saluran` | varchar | Channel (*saluran*): `walk_in`, `agent`, `online`, `telesales`. [`channel`] |
| `tgl_pengajuan` | timestamp | When the application was submitted (*tanggal pengajuan*). [`submitted_at`] |
| `tgl_putusan` | date | Date of the credit decision (*tanggal putusan*). Null while in process. [`decision_date`] |
| `sts_putusan` | varchar | Decision status: `approved`, `rejected`, `cancelled` (customer withdrew before a decision), `in_process`. [`decision_status`] |
| `req_amt` | decimal(18,2) | Amount the customer asked for. [`requested_amount`] |
| `appr_amt` | decimal(18,2) | Amount approved. Null unless approved. Often lower than requested. [`approved_amount`] |
| `req_tenor` | integer | Tenor in months the customer asked for. [`requested_tenor_months`] |
| `grade_risiko` | varchar | Risk grade `A` to `E`, assigned at decision. Null while in process. [`risk_grade`] |

### 3.5 `contracts` (one row per contract)

| Column | Type | Meaning |
|---|---|---|
| `contract_id` | integer | Primary key. |
| `application_id` | integer | References `loan_applications`. One approved application leads to at most one contract. |
| `customer_id` | integer | References `customers`. |
| `product_id` | integer | References `products`. |
| `booking_branch_id` | integer | Branch that originated the contract. Never changes. References `branches`. |
| `servicing_branch_id` | integer | Branch that services and collects the contract now. References `branches`. |
| `signed_date` | date | Date the customer signed. |
| `disbursement_date` | date | Date the money was released, 0 to 5 days after signing. Null for cancelled contracts. |
| `first_due_date` | date | Due date of the first installment. |
| `maturity_date` | date | Due date of the last installment under the current schedule. |
| `closed_date` | date | Date the contract was fully repaid or written off. Null while active. |
| `principal_amount` | decimal(18,2) | Principal financed. |
| `tenor_months` | integer | Number of installments at signing (12, 18, 24, 36, or 48). |
| `annual_rate_pct` | decimal(5,2) | Flat annual rate in percent. |
| `contract_status` | varchar | `active`, `closed`, `written_off`, `cancelled`. |
| `is_restructured` | boolean | True if the schedule was restructured at least once. |
| `restructured_date` | date | Date of the restructuring. Null if never restructured. |

### 3.6 `installment_schedules` (one row per scheduled installment, including replaced ones)

| Column | Type | Meaning |
|---|---|---|
| `schedule_id` | integer | Primary key. |
| `contract_id` | integer | References `contracts`. |
| `installment_no` | integer | Sequence number within its schedule version. |
| `schedule_version` | integer | 1 for the original schedule, 2 after a restructuring. |
| `is_current` | boolean | False for rows replaced by a restructuring. |
| `effective_from` | date | Date this row became part of the schedule (disbursement date or restructuring date). |
| `superseded_date` | date | Date this row was replaced. Null for current rows. |
| `due_date` | date | Date the installment falls due. |
| `principal_due` | decimal(18,2) | Principal part. |
| `interest_due` | decimal(18,2) | Interest part. |
| `installment_amount` | decimal(18,2) | `principal_due + interest_due`. |

### 3.7 `payments` (one row per payment received)

| Column | Type | Meaning |
|---|---|---|
| `payment_id` | integer | Primary key. |
| `contract_id` | integer | References `contracts`. |
| `schedule_id` | integer | The installment this payment was applied to. References `installment_schedules`. |
| `payment_date` | date | Date the customer paid, as reported by the channel. |
| `posting_date` | date | Date the payment was booked in the core system, 0 to 3 days after `payment_date`. |
| `amount_paid` | decimal(18,2) | `principal_paid + interest_paid + penalty_paid`. |
| `principal_paid` | decimal(18,2) | Part applied to principal. |
| `interest_paid` | decimal(18,2) | Part applied to interest. |
| `penalty_paid` | decimal(18,2) | Late payment penalty collected. Zero when paid on time. |
| `payment_channel` | varchar | `bank_transfer`, `virtual_account`, `retail_outlet`, `branch_cashier`, `collector`. |
| `is_reversed` | boolean | True for a payment that was later reversed (for example a failed transfer). The row stays. |

### 3.8 `collection_actions` (one row per collection activity)

Abbreviated, mixed-language column names (Q11). The semantic name each column should get is in brackets.

| Column | Type | Meaning |
|---|---|---|
| `act_id` | integer | Primary key. [`action_id`] |
| `kontrak_id` | integer | References `contracts`. [`contract_id`] |
| `tgl_aksi` | date | Date of the activity (*tanggal aksi*). [`action_date`] |
| `jns_aksi` | varchar | Type of activity (*jenis aksi*): `reminder_message`, `phone_call`, `field_visit`, `warning_letter`. [`action_type`] |
| `kolektor_id` | integer | Staff identifier (a number only; no staff table). [`collector_id`] |
| `hasil` | varchar | Outcome (*hasil*): `promise_to_pay`, `paid`, `no_contact`, `refused`. [`outcome`] |
| `tgl_janji` | date | Date the customer promised to pay (*tanggal janji*). Null unless the outcome is `promise_to_pay`. [`promise_date`] |
| `nom_janji` | decimal(18,2) | Amount promised (*nominal janji*). Null unless the outcome is `promise_to_pay`. [`promise_amount`] |

### 3.9 `contract_monthly_snapshots` (one row per contract per month end)

Not in the list in DAT-1; added on purpose and accepted by the owner (D-12): balances and days past due "as of a date" cannot be derived from schedules and payments with a simple aggregation, and real lenders report them from month-end positions. The generator derives this table from the other tables, so it is consistent with them.

| Column | Type | Meaning |
|---|---|---|
| `snapshot_date` | date | A month-end date. Part of the primary key. |
| `contract_id` | integer | References `contracts`. Part of the primary key. |
| `outstanding_principal` | decimal(18,2) | Principal still owed at the snapshot date. Always greater than zero (contracts with nothing owed have no row). |
| `days_past_due` | integer | Days since the oldest unpaid installment fell due; 0 if none is overdue. |
| `dpd_bucket` | varchar | `current` (0), `dpd_1_30`, `dpd_31_60`, `dpd_61_90`, `dpd_91_180`, `dpd_over_180`. |
| `collectibility` | varchar | `1` (0 to 10 days), `2` (11 to 90), `3` (91 to 120), `4` (121 to 180), `5` (over 180). |

### 3.10 Relationships

All are many-to-one from the first table to the second.

| From | Column | To | Relationship name in the semantic model |
|---|---|---|---|
| `customers` | `home_branch_id` | `branches` | `customer.home_branch` |
| `loan_applications` | `cust_id` | `customers` | `application.customer` |
| `loan_applications` | `prod_id` | `products` | `application.product` |
| `loan_applications` | `cab_id` | `branches` | `application.branch` |
| `contracts` | `application_id` | `loan_applications` | `contract.application` |
| `contracts` | `customer_id` | `customers` | `contract.customer` |
| `contracts` | `product_id` | `products` | `contract.product` |
| `contracts` | `booking_branch_id` | `branches` | `contract.booking_branch` |
| `contracts` | `servicing_branch_id` | `branches` | `contract.servicing_branch` |
| `installment_schedules` | `contract_id` | `contracts` | `installment.contract` |
| `payments` | `contract_id` | `contracts` | `payment.contract` |
| `payments` | `schedule_id` | `installment_schedules` | `payment.installment` |
| `collection_actions` | `kontrak_id` | `contracts` | `collection_action.contract` |
| `contract_monthly_snapshots` | `contract_id` | `contracts` | `contract_snapshot.contract` |

Entity names in the semantic model: `branch`, `product`, `customer`, `application`, `contract`, `installment`, `payment`, `collection_action`, `contract_snapshot`.

## 4. Quirks (DAT-2)

Each quirk is a way to get a wrong number from the raw tables. The generator must produce each one, and a test must check it (the "Check" column is an acceptance criterion for WP-02).

| # | Quirk | Why it matters | Check on the generated data |
|---|---|---|---|
| Q1 | **Several dates with different meanings.** Contracts have `signed_date`, `disbursement_date`, `first_due_date`, `maturity_date`, `closed_date`. Payments have `payment_date` and `posting_date`. Applications have `submitted_date` and `decision_date`. | "Disbursement in June" must use `disbursement_date`. Collections must use `posting_date`. | At least 3% of contracts are signed and disbursed in different months. At least 3% of payments have payment and posting dates in different months. |
| Q2 | **Cancelled contracts.** About 4% of contracts were signed and then cancelled before disbursement. They have a principal amount and a null `disbursement_date`. | Summing `principal_amount` over all contracts overstates disbursement. | Between 2% and 6% of contracts are `cancelled`; all have null `disbursement_date` and no schedule, payment, or snapshot rows. |
| Q3 | **Two branches per contract.** About 20% of contracts have been transferred, so `servicing_branch_id` differs from `booking_branch_id`. Customers also have a `home_branch_id`, and applications a `branch_id`. | "By branch" is ambiguous in raw SQL. Sales metrics use the booking branch; portfolio and collection metrics use the servicing branch. | Between 15% and 25% of disbursed contracts have different booking and servicing branches. |
| Q4 | **Restructured contracts.** About 9% of disbursed contracts were restructured. Their unpaid original schedule rows stay in the table with `is_current = false` and a `superseded_date`; new rows with `schedule_version = 2` replace them. | Summing `installment_amount` over all rows double counts. | Between 6% and 12% of disbursed contracts have `is_restructured = true`; each has at least one row with `is_current = false` and at least one with `schedule_version = 2`; for no contract does the sum of `principal_due` over current rows differ from `principal_amount` by more than 1.00. |
| Q5 | **Late and missing payments.** Each contract follows one behaviour: about 70% pay on time (within 3 days), 18% pay late (4 to 45 days), 7% pay very late and then recover, 5% stop paying. Late payments carry a penalty. | Produces realistic days past due, NPF, and collection rates. | The NPF ratio at 2026-06-30 is between 1% and 8%. The collection rate for the first half of 2026 is between 85% and 100%. |
| Q6 | **Reversed payments.** About 3% of payments are flagged `is_reversed = true`; most are followed by a successful payment. | Summing all payments overstates collections. | Between 2% and 4% of payments have `is_reversed = true`. |
| Q7 | **Balances are not additive over time.** Snapshots hold one row per contract per month end. | Summing `outstanding_principal` over a quarter triples the portfolio. | For every snapshot row, `outstanding_principal` equals `principal_amount` minus the sum of `principal_paid` of non-reversed payments with `posting_date` on or before the snapshot date, within 0.01. |
| Q8 | **Written-off and closed contracts leave the book.** A contract more than 210 days past due at a month end is written off at that month end (`contract_status = written_off`, `closed_date` set) and has no later snapshot rows. Fully repaid contracts are `closed` and have no later snapshot rows. | The active portfolio is the set of snapshot rows, not the set of contracts. The write-off row is a snapshot row (the contract is written off *at* that month end), so it must be left out of every balance and count that stands for the book (WP-04B, D-31): at 2026-06-30 the 15 contracts written off that day would otherwise add to the outstanding principal, the NPF and PAR 30 amounts, and the active count (NPF 2.99% with them, 2.69% without). | No snapshot row has a `snapshot_date` after its contract's `closed_date`. |
| Q9 | **Personal data.** `customers` has five personal columns (`full_name`, `national_id`, `phone_number`, `email`, `birth_date`). | They must be masked or refused (QRY-2). | Every `national_id` matches `^99\d{14}$`, every `phone_number` starts with `0899000`, every `email` ends with `@example.com`. |
| Q10 | **Region is not province.** `branches.region_name` is a sales territory; `branches.province` and `customers.province` are administrative provinces. `Jawa Barat` is both a region and a province. | "In Jawa Barat" is ambiguous without a definition. | At least one region contains branches in more than one province. |
| Q11 | **Poor catalog metadata** (D-14). Two tables have abbreviated, mixed Indonesian and English column names (sections 3.4 and 3.8). In the catalog export, some tables and columns have no description or a vague one, and some descriptions are in Indonesian (section 6). | Real catalogs look like this. Search over catalog columns and an agent reading the raw schema must cope with it; tidy metadata would flatter both arms of the agent comparison and the column search. | The generated catalog export matches section 6.1 exactly: the listed tables and columns have an empty or the listed vague or Indonesian description, and every other table and column has its English description. |

### How the snapshot table is derived

For each disbursed contract and each month end `E` from the disbursement month up to the earliest of 2026-06-30, the month the contract is fully repaid, and the month it is written off:

- `outstanding_principal` = `principal_amount` minus the sum of `principal_paid` over non-reversed payments with `posting_date <= E`. If the result is zero or less, no row is written and the contract is closed.
- The schedule in force at `E` is the set of rows with `effective_from <= E` and (`superseded_date` is null or `superseded_date > E`).
- An installment in force is unpaid at `E` if the non-reversed payments applied to it with `posting_date <= E` sum to less than its `installment_amount`.
- `days_past_due` = `E` minus the earliest `due_date` among unpaid installments in force with `due_date < E`; 0 if there is none.
- `dpd_bucket` and `collectibility` follow from `days_past_due` using the bands in section 3.9.

## 5. Metrics (DAT-3)

There are 23 approved metrics, plus one draft and one deprecated metric that exist to test governance rules. "Time" is the official date of the metric. "Branch" says which branch link is pinned (see `docs/ARCHITECTURE.md` section 7.3).

Each metric needs synonyms in English and Indonesian, at least two example questions, at least one example request (a question with the structured request that answers it, D-18), an owner team, and a caveat where one applies. Example requests must not repeat a golden question or its expected request (`docs/EVALS.md` section 2). The seven metrics marked with a star are already written in `semantic/schema/examples/lending/metrics/`; copy them unchanged. The entity files in `semantic/schema/examples/lending/entities/` are a starting point: copy them and add the missing relationships (for example `contract.application`), dimensions, and measures listed in this document.

### Origination (owner team `sales-performance`)

| # | Name | Type | Plain-language definition | Built from | Time | Branch |
|---|---|---|---|---|---|---|
| 1 | `application_count` | simple | Number of applications submitted. | count of `application` rows | submitted date | `application.branch` |
| 2 | `decided_application_count` | simple | Number of applications with a credit decision (approved or rejected). Withdrawn and in-process applications are excluded. | count of `application` where `decision_status IN ('approved','rejected')` | decision date | `application.branch` |
| 3 | `approved_application_count` | simple | Number of applications approved. | count of `application` where `decision_status = 'approved'` | decision date | `application.branch` |
| 4 | `approval_rate` | ratio | Share of decided applications that were approved. | `approved_application_count / decided_application_count` | decision date | `application.branch` |
| 5 | `disbursement_amount` ★ | simple | Principal released to customers. Cancelled contracts are excluded. | sum of `principal_amount` where `disbursement_date` is not null | disbursement date | booking |
| 6 | `disbursed_contract_count` | simple | Number of contracts whose money was released. | count of `contract` where `disbursement_date` is not null | disbursement date | booking |
| 7 | `average_ticket_size` | ratio | Average principal per disbursed contract. | `disbursement_amount / disbursed_contract_count` | disbursement date | booking |
| 8 | `new_customer_count` | simple | Number of customer records created. | count of `customer` rows | registration date | `customer.home_branch` |

### Portfolio (owner team `credit-risk`). All are month-end balances: grains are month, quarter, year, and a period longer than a month reports its last month end.

| # | Name | Type | Plain-language definition | Built from | Time | Branch |
|---|---|---|---|---|---|---|
| 9 | `outstanding_principal` ★ | simple | Principal customers still owe at a month end. Contracts written off at that month end are left out. | sum of `outstanding_principal` | snapshot date | servicing |
| 10 | `active_contract_count` | simple | Number of contracts with principal still owed at a month end. Contracts written off at that month end are left out. | count of `contract_snapshot` rows | snapshot date | servicing |
| 11 | `npf_outstanding_principal` ★ | simple | Principal owed on contracts more than 90 days past due, without the contracts written off at that month end. | sum of `outstanding_principal` where `days_past_due > 90` | snapshot date | servicing |
| 12 | `npf_ratio` ★ | ratio | Share of outstanding principal that is more than 90 days past due. | `npf_outstanding_principal / outstanding_principal` | snapshot date | servicing |
| 13 | `par30_outstanding_principal` | simple | Principal owed on contracts more than 30 days past due, without the contracts written off at that month end. | sum of `outstanding_principal` where `days_past_due > 30` | snapshot date | servicing |
| 14 | `par30_ratio` | ratio | Share of outstanding principal that is more than 30 days past due ("portfolio at risk"). | `par30_outstanding_principal / outstanding_principal` | snapshot date | servicing |
| 15 | `restructured_outstanding_principal` | simple | Principal owed on contracts that have been restructured, without the contracts written off at that month end. The flag is the contract's current one: positions before the `restructured_date` are counted as restructured too (D-31). | `contract_snapshot.outstanding_principal` with the metric filter `{rule: contract.restructured}` (a rule across a join) | snapshot date | servicing |
| 16 | `restructured_ratio` | ratio | Share of outstanding principal on restructured contracts. | `restructured_outstanding_principal / outstanding_principal` | snapshot date | servicing |

### Collection (owner team `collection`)

| # | Name | Type | Plain-language definition | Built from | Time | Branch |
|---|---|---|---|---|---|---|
| 17 | `due_amount` ★ | simple | Installments (principal plus interest) falling due under the current schedule. | sum of `installment_amount` where `is_current` | due date | servicing |
| 18 | `collected_amount` ★ | simple | Principal and interest received, without penalties and reversed payments. | sum of `principal_paid + interest_paid` where not reversed | posting date | servicing |
| 19 | `collection_rate` ★ | ratio | Amount collected divided by amount due in the same period. | `collected_amount / due_amount` (two different tables) | posting date and due date | servicing |
| 20 | `penalty_income` | simple | Late payment penalties received. | sum of `penalty_paid` where not reversed | posting date | servicing |
| 21 | `collection_action_count` | simple | Number of collection activities carried out. | count of `collection_action` rows | action date | servicing |
| 22 | `promise_to_pay_count` | simple | Number of collection activities that ended with a promise to pay. | count of `collection_action` where `outcome = 'promise_to_pay'` | action date | servicing |
| 23 | `promise_to_pay_rate` | ratio | Share of collection activities that ended with a promise to pay. | `promise_to_pay_count / collection_action_count` | action date | servicing |

### Governance fixtures (not counted in the 23)

| Name | State | Purpose |
|---|---|---|
| `write_off_amount` | draft, file `write_off_amount.draft.yaml`, `drafted_by: ai:sample-assistant` | A draft with no approved version. Sum of `outstanding_principal` at the last snapshot of contracts that became `written_off`. Must be visible to curators, ranked below approved results, hidden from agents by default, and refused by the compiler. |
| `npl_ratio` | deprecated, `replaced_by: metric:npf_ratio`, synonyms include "NPL" and "kredit macet" | An older name and report for the share of non-performing financing, with the same inputs as `npf_ratio`, kept so that old questions lead to the official metric. Must be hidden from search by default and must not outrank `npf_ratio`. |

### Rules (D-17)

Conditions that several measures or metrics share are written once, as named rules on an entity:

| Rule | Condition | Used by |
|---|---|---|
| `contract_snapshot.non_performing` ★ | `days_past_due > 90` | `npf_outstanding_principal` (and through it `npf_ratio`). In everyday use "kredit macet" means this rule; the collectibility class "Macet" is class 5 only, more than 180 days (D-31). |
| `contract_snapshot.past_due_30` | `days_past_due > 30` | `par30_outstanding_principal` (and `par30_ratio`) |
| `contract.restructured` | `is_restructured` | `restructured_outstanding_principal` (a metric filter across a join, and `restructured_ratio`). It reads the current flag of the contract, not its state at the snapshot date; comparing `restructured_date` with the snapshot date cannot be written in a definition (a phase 2 candidate, D-31). |
| `contract_snapshot.in_book` | `COALESCE(days_past_due, 0) <= 210` | A metric filter of `outstanding_principal`, `active_contract_count`, `npf_outstanding_principal`, `par30_outstanding_principal` and `restructured_outstanding_principal` (and through them the three ratios). It leaves out exactly the write-off row, which is a snapshot row (quirk Q8); a missing value counts as in the book (the sample data has none). The draft `write_off_amount` does not use it: it is the one measure meant to see the write-off row. |
| `contract_snapshot.write_off_position` | `days_past_due > 210` | the draft `write_off_amount` (governance fixture). It selects exactly the write-off row: a contract is written off at the first month end past 210 days and has no later snapshot rows (quirk Q8). |

### Glossary terms

At least these eight, each with English and Indonesian synonyms and links to related metrics: `npf` ★, `days_past_due` (DPD, "hari keterlambatan", "tunggakan"), `collectibility` ("kolektibilitas", "kol"), `disbursement` ("pencairan"), `restructuring` ("restrukturisasi"), `outstanding` ("baki debet", "sisa pokok"), `tenor`, `portfolio_at_risk` (PAR).

### Dimensions that must exist

Beyond those in the example entity files: on `application`: `submitted_date` (time, `CAST(tgl_pengajuan AS DATE)`), `decision_date` (time, column `tgl_putusan`), `decision_status` (`sts_putusan`), `channel` (`saluran`), `risk_grade` (`grade_risiko`). On `collection_action`: `action_date` (time, `tgl_aksi`), `action_type` (`jns_aksi`), `outcome` (`hasil`). Dimension names are always clean English; the YAML descriptions and synonyms are complete even where the catalog's are poor (that gap is what a semantic layer adds). On `customer`: `gender`, `income_band`, `city`; `email` as personal with no masking. On `branch`: `branch_code`, `is_active`. On `payment`: nothing more. On `contract`: `annual_rate_pct` (numeric), `closed_date` (time), `restructured_date` (time).

## 6. Catalog export

WP-06 writes `data/catalog/lending.catalog.yaml`, the file-based catalog export (CON-3) for this domain: every table and column above with its type and catalog description, owners, the `PII.Sensitive` tag on the five personal columns, and the glossary terms. The format is defined in `docs/ARCHITECTURE.md` section 6.2. The export is generated from the WP-02 table specification, which holds two texts per table and column: the true meaning (section 3) and the catalog description (this section).

### 6.1 Catalog description quality (Q11, D-14)

By default a table's or column's catalog description is the English meaning from section 3, shortened to one sentence. The exceptions below are deliberate, so that search and the raw-schema arm of the agent comparison face a realistic catalog. They are fixed (not random), so tests and evals are reproducible.

| Object | Catalog description |
|---|---|
| table `loan_applications` | `LOS extract` (vague) |
| table `installment_schedules` | none (empty) |
| table `collection_actions` | none (empty) |
| table `contract_monthly_snapshots` | `Data bulanan kontrak` (Indonesian, vague: "monthly contract data") |
| every column of `collection_actions` | none (empty) |
| `loan_applications`: `saluran`, `req_amt`, `appr_amt`, `req_tenor`, `grade_risiko` | none (empty) |
| `loan_applications`: `sts_putusan` | `Status` (vague) |
| `loan_applications`: `tgl_pengajuan`, `tgl_putusan` | `Tanggal` (Indonesian, vague: "date") |
| `installment_schedules`: `schedule_version`, `is_current`, `superseded_date`, `effective_from` | none (empty) |
| `payments.posting_date` | `Tgl posting` (Indonesian, abbreviated) |
| `payments.is_reversed` | `Flag` (vague) |
| `contracts.servicing_branch_id` | `Cabang` (Indonesian, vague: "branch") |
| `contracts.disbursement_date` | `Tanggal pencairan dana` (Indonesian: "date the funds were disbursed") |
| `contract_monthly_snapshots.collectibility` | `Kol` (abbreviation) |
| `contract_monthly_snapshots.days_past_due` | `DPD` (abbreviation) |
| `customers.income_band` | `Range penghasilan per bulan` (mixed Indonesian and English: "monthly income range") |

All four tables in this list still have owners, and the five personal columns of `customers` keep their English descriptions and their `PII.Sensitive` tag: poor descriptions must not weaken the sensitivity rules, which come from tags (`docs/ARCHITECTURE.md` section 7.6).

The YAML definitions (WP-04) do not copy these defects: their descriptions and synonyms are complete. The difference between what the catalog says and what the definitions say is part of what the evals measure.
