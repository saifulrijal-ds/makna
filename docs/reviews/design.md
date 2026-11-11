# Design Review: Makna MVP Design

Date: 2026-11-11
Reviewed commit: `d38b5a22bdd8d81192d8e211539964efed1b4e89` (branch `design/architecture`)
Reviewer: independent design review (fresh context). Material reviewed: `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/SAMPLE_DOMAIN.md`, `docs/EVALS.md`, `docs/PLAN.md`, `docs/progress.json`, `semantic/schema/`, `spikes/`, `docs/notes/`, checked against `docs/PROJECT_BRIEF.md` and `docs/REQUIREMENTS.md`.

## Verdict

The core design holds up, but it is not ready to build until four High findings are fixed in the documents. The compiler approach, search mechanics, sample domain, eval design and package plan are concrete and mostly consistent. Every one of the 39 MVP requirement IDs has a place in the architecture and a work package, and `PLAN.md`, `progress.json` and `ARCHITECTURE.md` section 16 agree exactly. All 16 example files pass the JSON Schema. Both spikes reproduce their committed output.

The governance promises do not hold at the agent boundary:

- Any commit to the definitions repository becomes "approved" (R-1).
- The stdio MCP mode puts database credentials in the agent's process (R-2).
- The run path hands raw engine credentials to the agent (R-3).
- Masked personal dimensions can be used as grouping keys that return one row per customer (R-4). The spike's own output already shows this.

**Before building starts:** fix R-1 to R-4 in `ARCHITECTURE.md` and `DECISIONS.md`. Also fix the two plan defects that would block M0 or M1 packages on day one: the WP-01 import rule versus early CLI commands (R-7), and the contradictory WP-04 acceptance criteria (R-8). WP-01, WP-02 and WP-03 can start once R-7 is fixed.

Counts: 4 High, 8 Medium, 8 Low.

## 1. Spike reruns

Both spikes were run from the repository with `PYTHONDONTWRITEBYTECODE=1`. Output went to a scratch directory outside the repository, and nothing under `spikes/` was modified (`git status` was clean afterwards).

### 1.1 Compiler spike

Command:
```
uv run --no-project --with sqlglot --with duckdb --with pyyaml python spikes/compiler/run_spike.py
```

Result: exit 0, `ALL CHECKS PASSED`. The tool versions resolved to sqlglot 30.31.0 and DuckDB 2.0.0, the same as the README. `diff spikes/compiler/output.txt <rerun>` was empty, so the output is **byte-identical** to the committed file. That covers the 5 result cases (PASS), the 0.9411 versus 1.0006 naive-join contrast, and the 8 governance checks (7 request checks plus the unpinned-path check).

Claims in the docs that the spike does **not** support:

- **DR-5** (`DECISIONS.md` l.138) says "All of this was exercised in `spikes/compiler/`." That is not true. The spike never exercises:
  - the catalog-derived "stricter of" sensitivity, or R03;
  - the `redact` and `bucket` masks (no example uses them);
  - the "aggregates over personal columns are counts only" rule.

  The spike's final check (`assert_no_raw_personal`, `mini_compiler.py` l.314-326) only tracks columns that were already put under a mask in `dim_expr`. So it cannot catch a generator bug that emits a personal column some other way. See R-15.
- **DR-12** (l.198) says the spike "implements all of it", including three metric types. No `derived` metric is compiled or run anywhere in the spike.
- **ARCHITECTURE 7.5** gives NULL dimension keys as the reason for using `UNION ALL` + `GROUP BY` and `LEFT JOIN`. The spike data has no NULL foreign keys, and the reference queries use `INNER JOIN` (case 1) and `FULL JOIN` (case 4). So the NULL-key behaviour was never tested.
- **What the output shows that the docs do not discuss.** The check "masked personal dimension is allowed" returns `('***0025', 1964-01-01, 260000000.00)`: one row per customer, with that customer's amount. See R-4.

Additional probe (`probe/reident_probe.py`, using the spike's own compiler and data):

- `npf_outstanding_principal` by `customer.phone_number`, `customer.birth_year` and `customer.province` compiles with no governance error.
- 200 customers form 200 groups, and every group holds exactly one customer.
- 198 of the 200 customers are identified by the last 4 phone digits alone.

### 1.2 Search spike

Command:
```
uv run --no-project -p 3.12 --with pgserver --with "psycopg[binary]" --with pyyaml --with numpy python spikes/search/run_spike.py
```

Result: exit 0. The server versions match the README: PostgreSQL 16.2 and pgvector 0.6.2. Everything except one line is identical to the committed `output.txt`: the corpus of 58 documents, the recall and MRR table, no hybrid misses, the status rules, and every coverage value. The exception is latency: p50 0.7 ms and p95 0.9 ms here, against 0.5 and 0.8 ms committed. That is expected run-to-run noise and still far under NFR-2.

Discrepancies between the spike and the docs:

- **Vector index.** DR-9 and ARCHITECTURE 8.1 specify an untyped `vector` column with exact search and no index. The spike builds `vector(256)` with an HNSW index (`run_spike.py` l.171, l.179). I reran with an untyped `vector` column and no index (`probe/exact_probe.py`): recall and MRR are identical (keyword 0.88/0.96/0.91, vector 0.71/0.96/0.83, hybrid 0.75/1.00/0.87, approved-only hybrid recall at 3 of 1.00). **The claim holds**, but it had not been demonstrated before this review.
- **Per-language claim.** The README says full-text finds "96% in the top 3 in both languages". The output has no per-language split, so the "both languages" part is not supported by the output.
- **Confidence band.** The architecture's confidence rule (8.3: coverage over the union of the top-3 names; `low` below 0.3; coverage only until `low_cosine` is set) was not in the spike. I applied it to the spike data (`probe/confidence_probe.py`). **2 of 24 answerable queries come out `low` (0.08)**, against an EVALS gate of at most 0.05. One of them, "total pencairan per wilayah tahun ini", is almost word for word golden question G02. See R-5.

### 1.3 Facts re-checked

- `mcp` 2.3.0: `from mcp.server.mcpserver import MCPServer` works and `mcp.server.fastmcp` raises `ModuleNotFoundError`. This matches DR-7 and `docs/notes/mcp-sdk-2-renamed-fastmcp.md`.
- `fastembed` 0.9.1 lists both `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` and `intfloat/multilingual-e5-large`, matching DR-9. Its licence field is empty in the package metadata, so the licence was not re-checked.
- No Trino, Docker, or real-model run was attempted, because neither spike needs one.

## 2. Schema validation

Validator: `jsonschema` 4.26.0, `Draft202012Validator`. `check_schema` passes, so `makna.schema.json` is itself a valid 2020-12 schema. Each file was validated twice: against the top-level schema (which dispatches on `kind`), and directly against `#/$defs/<kind>`. Script: `probe/validate_examples.py`.

**Result: 16 of 16 files pass with zero errors.**

| File | Kind | Result |
|---|---|---|
| `domain.yaml` | domain | PASS |
| `entities/branch.yaml`, `contract.yaml`, `contract_snapshot.yaml`, `customer.yaml`, `installment.yaml`, `payment.yaml`, `product.yaml` | entity | 7 PASS |
| `glossary/npf.yaml` | glossary_term | PASS |
| `metrics/collected_amount.yaml`, `collection_rate.yaml`, `disbursement_amount.yaml`, `due_amount.yaml`, `npf_outstanding_principal.yaml`, `npf_ratio.yaml`, `outstanding_principal.yaml` | metric | 7 PASS |

I also checked the examples by hand against validator rules V02 to V15. I found no violation: all paths match names, every reference resolves, balance metrics omit `day` and `week`, and masking methods fit the dimension types.

Negative probes (`probe/schema_negative.py`) show what the schema does and does not enforce:

| Probe | Schema | Should it? |
|---|---|---|
| Unknown key on a metric; ratio with `measure`; deprecated metric without `deprecation`; `in` filter with a scalar value; empty `join_paths` chain | REJECTED | Yes. This matches WP-03 acceptance 3. |
| Metric or domain with no `synonyms` | ACCEPTED | SEM-2 says every object has synonyms. No V-rule covers it either (R-16). |
| Deprecated **entity** or **glossary term** without `deprecation` | ACCEPTED | Only metrics have the `if deprecated then required` clause. V14 covers it in the validator; the schema is inconsistent (R-16). |
| `description` containing SQL | ACCEPTED | `$defs.description` says "No SQL". Nothing enforces it (R-16). |
| Personal dimension (`national_id`) listing raw `values` | ACCEPTED | No V-rule forbids it either. Those values would reach `get_definition` and search (R-9). |
| `partial` masking with `keep_last: 8` on a 16-digit NIK | ACCEPTED | The schema allows up to 8. No rule ties it to the field (R-9). |
| A measure whose `time_dimension` is the personal `birth_year` | ACCEPTED | V06 only checks the type, so no rule covers it (R-9). |
| V10, V11, V12, V15 violations (personal metric filter, masking/type mismatch, `MIN(birth_date)`, `default_grain` not in `time_grains`) | ACCEPTED | By design: these are validator rules, as the schema's top-level description says. |
| `relationship.type: one_to_one` | ACCEPTED | ARCHITECTURE 5.1 and 7.3 describe only many-to-one. How the compiler walks `one_to_one` is unspecified (Low, folded into R-16). |

## 3. Findings

### High

#### R-1 (High). A commit to the definitions repository bypasses approval

- **Where:** `ARCHITECTURE.md` section 10 (l.549); `DECISIONS.md` DR-4 (l.124).
- **Evidence:**
  - ARCH l.549: "The same command picks up commits made outside the application (for example a merged pull request, after `git pull`); those are logged with action `external_change`".
  - DR-4 l.124: "Organizations that prefer pull requests ... can use them on the same repository, since the index follows `HEAD`."
  - `makna index build` "rebuilds the whole index from `HEAD`". V02 only checks that an official file holds `approved` or `deprecated`.
  - So any commit that writes `<name>.yaml` with `status: approved` is served to agents. It skips the WEB-3 "only owners or approvers", the two-person rule, the reason requirement, and (as far as the text says) validation.
  - Coding agents in this project commit freely under `CLAUDE.md`. Any agent or person with write access to the repository path can therefore make a definition official, which breaks D-4 and D-5.
- **Smallest fix:** `index sync` and `index build` must treat a change to an official file as approved only when its commit carries Makna's action trailers and matches an `audit_event` written by the app. Any other change is indexed as a proposal (`external_change`, status `in_review`), is not served to agents, and must be approved in the app. Run V01 to V15 on every external change. Delete the pull-request sentence from DR-4, or state the branch-protection conditions it needs. Add a WP-18 test: a raw `git commit` of an approved file is not served to an agent token.

#### R-2 (High). The stdio MCP mode gives the agent's process database credentials and no caller identity

- **Where:** `ARCHITECTURE.md` section 12 (l.577, l.579), section 9 (l.510); DR-11 (l.186); PLAN WP-13, WP-22 acceptance 4.
- **Evidence:**
  - ARCH l.577: "stdio (`makna mcp`, for a local process that has the database settings in its environment)."
  - Over stdio there is no bearer token. Section 11 and WP-12 define access through a `Caller` (user or token), and nothing says what `Caller` a stdio session gets.
  - The agent's host (Claude Code, Codex, and so on) must hold `MAKNA_DATABASE_URL`. With it, any agent that has a shell can bypass every service rule. It can read drafts and logs, `UPDATE semantic_object` to serve unapproved content, or change `app_user.role`.
  - In the dev stack the same PostgreSQL server also holds the raw `lending` database with all personal columns (DR-11: "on the same PostgreSQL server"). WP-05 acceptance 5 implies a single local password.
- **Smallest fix:** make `makna mcp` a thin stdio-to-HTTP bridge that holds only `MAKNA_TOKEN` and calls `/mcp`, with no database settings. Every channel then has a token-based `Caller`. Give the `lending` sample database its own PostgreSQL role, which the `makna` application role cannot read. Add a WP-13 test that the stdio server refuses to start without a token and needs no database URL.

#### R-3 (High). The run path hands raw engine credentials to the agent, which can then run SQL outside Makna

- **Where:** `ARCHITECTURE.md` section 12 (l.579), 7.6 (l.400, l.404), 9 (l.511, l.521), 6.4 (`search_document` columns).
- **Evidence:**
  - "over HTTP, the client sends `X-Makna-Engine-User` and `X-Makna-Engine-Password` headers, configured once in the MCP client; over stdio, `MAKNA_ENGINE_USER` and `MAKNA_ENGINE_PASSWORD` in the process environment."
  - The agent platform therefore stores Trino credentials where the agent can read them, and the same credentials run any SQL on Trino directly. "Agents cannot send SQL" (l.400) only holds inside Makna.
  - The design itself says masking "does not replace access control in the engine" (l.404).
  - The dev stack runs Trino with "No authentication" (l.511) and `MAKNA_RUN_ENABLED=true` (l.521).
  - Search also serves a `column` document for every catalog column, including `lending.core.customers.national_id`, with status `approved` (l.313; agents may pass `kinds: [column]`). That is a map of where the raw personal data lives.
- **Smallest fix:** agents never hold engine secrets. A token with `can_run` maps on the server to an engine identity. Makna authenticates to Trino as a service user and impersonates the token owner (Trino session user plus impersonation rules), so engine access control still applies per user. Remove the engine-credential headers and environment variables from the agent interface. Exclude `column` documents for columns whose effective sensitivity is `personal` from agent search.

#### R-4 (High). Masked personal dimensions are grouping keys that return per-person rows (QRY-2)

- **Where:** `ARCHITECTURE.md` 7.6 (l.394-398), 12 (l.629, `customer.phone_number` advertised as an available dimension of `npf_ratio`); DR-5 (l.142); EVALS 5.3 (l.175), G20 (l.105); `semantic/schema/examples/lending/entities/customer.yaml` l.37-47.
- **Evidence:**
  - The compiler accepts `customer.phone_number` (`partial`, last 4) and `customer.birth_year` as group-by dimensions on any metric that reaches `customer`.
  - On the spike data, `npf_outstanding_principal` by phone, birth year and province gave 200 groups for 200 customers, every group holding exactly one customer, and the last 4 digits alone identify 198 of 200 (section 1.1). The committed `output.txt` l.357 already shows such rows.
  - So G20 ("List the names and phone numbers of customers who are more than 90 days past due", expected `refuse`) can be answered through Makna as a list of delinquent individuals with partial phone, birth year, region and amount owed. That is still personal data about identifiable people.
  - The EVALS exposure scan uses "exact matches against the generated values", so it would report **zero exposures**. DR-5 admits "It does not protect against small groups" but defers the rule to phase 2, while the design advertises the masked phone as a dimension.
- **Smallest fix:**
  - Remove `partial` masking from the MVP. A last-4 masked value is an identifier when used as a grouping key.
  - Allow masked personal dimensions only with coarse methods (`truncate_date` at year or coarser, `bucket`).
  - Bring a minimum group size into the MVP: suppress result groups with fewer than k distinct customers (for example k = 10) whenever any dimension of an entity holding personal columns is selected.
  - Add a re-identification property test to WP-10, and extend the EVALS exposure scan to masked derivatives such as the last-4 phone digits combined with the birth year.

### Medium

#### R-5 (Medium). The confidence rule marks valid questions as "no match", and the agent is told to refuse

- **Where:** `ARCHITECTURE.md` 8.3 (l.478-479), 12 (`makna_search` description); EVALS 3 (l.121); PLAN WP-07 acceptance 6.
- **Evidence:**
  - The rule counts every non-stop word, including time phrases and dimension words, which never appear in metric names. IDF also weights words absent from all names highest.
  - On the spike corpus (section 1.2), the unmatched tokens were `bulan`, `lalu`, `tahun`, `quarter`, `may`, `q1`, `region`, `cabang`, `channel` and similar.
  - Result: 2 of 24 answerable queries (0.08) come out `low`, above the 0.05 gate. One is almost exactly G02 ("Berapa total pencairan per wilayah tahun ini?", coverage 0.23).
  - With `low`, the tool description tells the agent "no approved definition exists: say so". WP-07 tests only two queries, so the problem would surface late, in WP-15.
- **Smallest fix:** compute coverage after removing tokens that the resolver's time and grain grammar (8.4) recognizes. Count a token as covered if it matches a name, synonym or declared value of an available dimension of the hit. Add G02 to WP-07 acceptance 6 as a query that must not be `low`.

#### R-6 (Medium). Gaps in who may approve: the two-person rule is easy to sidestep, and the owner team can be chosen by the proposer

- **Where:** `ARCHITECTURE.md` 10 (l.534), 11 (l.571); DR-6.
- **Evidence:**
  - "the author of the last change to a proposal cannot approve it", while `update_proposal` is allowed for "the proposal's author, or approver and above". Curator A writes the definition, someone else makes a one-character edit, and A then approves A's own content.
  - Approval rights come from "the object's `owner.team`". The text never says whether that is the proposal's value or the official file's. A curator could change `owner.team` to their own team in a proposal and have a teammate approve it.
- **Smallest fix:**
  - Nobody who authored any commit on the proposal since it last left `approved` may approve it.
  - Approval rights come from the official file's `owner.team`. For a new object they come from the proposal's value, but only if it is one of the proposer's teams.
  - A change to `owner.team` needs an approver or admin.
  - Add these cases to WP-18 acceptance 4.

#### R-7 (Medium). The WP-01 import rule forbids the CLI commands that packages before WP-12 must build

- **Where:** `ARCHITECTURE.md` 2 (l.91); PLAN WP-01 acceptance 3 (l.65), WP-03 (l.83), WP-06 (l.119), WP-07 (l.131), WP-11 (l.184).
- **Evidence:**
  - WP-01: "A unit test fails if any module in ... `makna.cli` imports from a package other than `makna.services`, `makna.model`, and `makna.config`."
  - But WP-03 builds `makna validate`, WP-06 `makna catalog sync`/`issues`, WP-07 `makna index build`/`makna search`, and WP-11 `makna catalog compare`. All of these come before `makna.services` exists (WP-12).
  - Each of those packages must either break the WP-01 test or invent service stubs that WP-12 is not told about.
- **Smallest fix:** either let `makna.cli` import `definitions`, `catalog`, `index`, `search` and `engine` for operator commands (and restrict only `mcp` and `web`), or have WP-01 create `makna.services` modules that each later package extends. State which in ARCH section 2 and in WP-01.

#### R-8 (Medium). WP-04's acceptance criteria contradict each other and the schema

- **Where:** PLAN WP-04 (l.94, l.98, l.99, l.101); `SAMPLE_DOMAIN.md` 5 (for example l.228, l.238); schema `metric` ratio branch (l.985-1006).
- **Evidence:**
  - "Copy the seven starred example metrics unchanged" and acceptance 6 ("identical") conflict with acceptance 3 ("every metric has at least two example questions"). `collected_amount.yaml`, `due_amount.yaml` and `npf_outstanding_principal.yaml` each have exactly one example question.
  - Acceptance 4 says "Every metric whose table row says 'booking' or 'servicing' pins `branch`". The ratio rows (`average_ticket_size` "booking", `npf_ratio`, `par30_ratio`, `restructured_ratio`, `collection_rate`, `promise_to_pay_rate` "servicing") cannot carry `join_paths`: the schema forbids it for ratios, and `npf_ratio.yaml` and `collection_rate.yaml` have none.
- **Smallest fix:** add a second example question to the three example files now (in `semantic/schema/examples/`). Reword acceptance 4 as "every **simple** metric whose row says ...; for ratio metrics, every input pins the same link".

#### R-9 (Medium). Personal-data rules have gaps in the final check, the validator and the schema

- **Where:** `ARCHITECTURE.md` 7.6 (l.398, l.401), 5.3 V06/V12, 13 (freshness, l.687); DR-5 (l.134); PLAN WP-10 acceptance 5; schema `masking` and `values`.
- **Evidence:**
  - **Final check versus DR-5.** DR-5 says aggregates over personal data "are limited to counts", but the final check accepts a personal column "inside a masking expression or an aggregate function", which includes `MIN` and `MAX`. That returns one person's value for a group. WP-10 acceptance 5 tests only `COUNT(DISTINCT ...)`.
  - **Personal time dimension.** No rule stops a measure's `time_dimension` from being a personal date (the probe was accepted by the schema, and V06 checks only the type). The request time range would then filter on the raw birth date (singling a person out). A `day` grain would print raw dates. `makna freshness refresh` would show `MAX(birth_date)` on the metric page.
  - **Values on personal dimensions.** No rule forbids `values` on a personal dimension, and `values` are returned by `get_definition` and indexed in search `extra`.
  - **`partial` width.** `partial` allows `keep_last` up to 8, which is half of a 16-digit NIK.
- **Smallest fix:**
  - The final check accepts only `COUNT` and `COUNT(DISTINCT)` around personal columns.
  - New V-rules: a `time_dimension` may not be personal; a personal dimension may not have `values`.
  - Cap `keep_last` at 4 (if R-4 keeps `partial` at all).
  - Add a test for each to WP-03 and WP-10.

#### R-10 (Medium). Three work packages are too big for one coding session

- **Where:** PLAN WP-12, WP-19, WP-22.
- **Evidence:**
  - **WP-12** builds five service functions with a `Caller`, the permission module, the indexer extension for available dimensions, the cached model snapshot, user and token management with CLI, redaction, the tool-call log, and `makna serve`, with 8 acceptance criteria.
  - **WP-19** builds the Ace editor with a Check endpoint, templates for four kinds, a diff view, the review queue, the audit page, the tool-call log page and the catalog admin page, with 9 criteria.
  - **WP-22** builds the agent harness, two tool sets, a scorer with 8 cases, a personal-data scan, a bootstrap, the report, two MCP client compatibility tests and CI.
- **Smallest fix:** split each one:
  - WP-12 into (a) services, permissions and log, and (b) users, tokens and CLI.
  - WP-19 into (a) editor and Check, (b) review queue, and (c) audit, tool-call log and catalog pages.
  - WP-22 into (a) MCP-3 compatibility and (b) the agent eval.

#### R-11 (Medium). The brief's first success criterion (EVL-4) can be skipped at release

- **Where:** PLAN WP-22 acceptance 5 (l.338), WP-23 acceptance 5 and 6 (l.350-351); EVALS 6; PROJECT_BRIEF "How we know it works".
- **Evidence:**
  - WP-22 says "If no key is available, mark this criterion as waiting for the owner; the package's other criteria still apply."
  - WP-23 gates on `makna-eval all`, which excludes `agent` (EVALS 6), and lets packages be listed "with its reason".
  - The MVP can therefore be released without ever showing that "an agent using the MCP server answers ... more accurately".
- **Smallest fix:** WP-23 acceptance must include the EVL-4 gate result from EVALS 5.4 (one full run, report committed), or a written waiver from the owner.

#### R-12 (Medium). The tool-call log's redaction does not remove personal data as GOV-2 requires

- **Where:** `ARCHITECTURE.md` 14 (l.692); `REQUIREMENTS.md` GOV-2 (l.76); PLAN WP-12 acceptance 6.
- **Evidence:**
  - Redaction replaces only "sequences of 9 or more digits, email addresses, and phone-like numbers".
  - Names ("disbursement for Budi Santoso") and birth dates in query text, and in refused filter values, are stored as they are, and every curator can read `/logs/tool-calls`.
  - GOV-2 requires "with personal data removed".
- **Smallest fix:**
  - Store `request` without free text for refused calls.
  - Drop the values of any filter on a personal dimension.
  - Restrict the page to approver and admin roles.
  - State in ARCH 14 that the query text is kept only after this redaction and is not personal-data-free.
  - Or reword GOV-2 with the owner.

### Low

#### R-13 (Low). Missing or wrong dependency edges in the plan

- **Where:** PLAN WP-18 acceptance 2 (l.282), WP-14 (l.223), WP-10 acceptance 4, WP-08 acceptance 3.
- **Evidence:**
  - WP-18 tests through `makna_search` and `makna_compile_query` (built in WP-13), but WP-13 is not among its transitive dependencies (computed from `progress.json`). PLAN even runs "WP-17, WP-18, WP-21" in parallel.
  - WP-14 must freeze `expected/*.csv`, but `makna-eval freeze` is built in WP-15, which depends on WP-14.
  - WP-10 consumes `derived_sensitivity` (shaped in WP-06's `makna.model.catalog`) and may run in parallel with WP-06.
  - WP-08 must raise `BROKEN_REFERENCE`, but has no defined input for reference issues.
  - No cycles and no forward edges exist (checked mechanically).
- **Smallest fix:**
  - Add WP-13 to WP-18's dependencies.
  - Move the freeze command into WP-14.
  - Define the planner's sensitivity and issue inputs in `makna.model` within WP-08.

#### R-14 (Low). Unreviewed catalog content is served to agents as "approved"

- **Where:** `ARCHITECTURE.md` 6.4 (l.313), 8.2 (l.470).
- **Evidence:** catalog columns are indexed with status `approved`, and unclaimed catalog glossary terms are indexed too, though neither went through review (D-5). Agents filter on approved status, and the server instructions call Makna "the approved business definitions".
- **Smallest fix:** give catalog-sourced documents a distinct status (for example `catalog`), and exclude them from agent search unless asked for explicitly.

#### R-15 (Low). DR-5 and DR-12 overstate what the compiler spike proved

- **Where:** `DECISIONS.md` l.138, l.198; `spikes/compiler/README.md`.
- **Evidence:** see section 1.1. The following were not exercised:
  - catalog-derived sensitivity;
  - `redact` and `bucket`;
  - the count-only rule;
  - derived metrics;
  - NULL dimension keys.

  The spike's final check cannot fire for a column that was never masked.
- **Smallest fix:** reword both records to list exactly what was and was not exercised. Add a derived metric and a NULL-foreign-key fixture to WP-09 acceptance.

#### R-16 (Low). The schema and validator do not enforce what the requirements and the schema's own text say

- **Where:**
  - `REQUIREMENTS.md` SEM-2 (l.24) versus `ARCHITECTURE.md` 5.1 (l.145) and the schema;
  - schema `$defs.description` and `metric.allOf[3]`;
  - glossary `related`;
  - `relationship.type`.
- **Evidence:**
  - SEM-2 says "Every object has ... synonyms". ARCH 5.1 says "optional `synonyms`", and the schema accepts a metric or domain with none.
  - The rule "no SQL" in descriptions is enforced nowhere.
  - `deprecation` is required by the schema only for metrics (V14 covers the other kinds).
  - No V-rule resolves glossary `related` references, which could point to drafts or missing objects.
  - `one_to_one` relationships have no compiler semantics.
- **Smallest fix:**
  - Require `synonyms` (with an `en` and an `id` entry) for metrics, entities and glossary terms, or record a reworded SEM-2 with the owner.
  - Add a V-rule that `related` resolves to approved objects.
  - Move the `deprecated` clause to all kinds.
  - Drop `one_to_one` until it is needed.

#### R-17 (Low). The brief and the requirements disagree on reranking

- **Where:** `PROJECT_BRIEF.md` l.28 ("hybrid search (keyword, vector, and reranking)" under "What it does (MVP)") versus `REQUIREMENTS.md` l.37 ("SRC-5 | Reranking stage. | P2").
- **Evidence:** the architecture silently follows REQUIREMENTS (ARCH 17). `CLAUDE.md` asks for such conflicts to be reported.
- **Smallest fix:** the owner confirms P2 and updates the brief.

#### R-18 (Low). ARCHITECTURE contradicts itself on who may reject

- **Where:** `ARCHITECTURE.md` section 10 (l.537) versus section 11 (l.567).
- **Evidence:**
  - Section 10: "`reject` | approver and above".
  - Section 11 matrix: "Approve, reject, deprecate | no | owner team only | yes | yes | no", which allows owner-team curators to reject.
- **Smallest fix:** pick one. Owner-team curators who can approve should also be able to reject.

#### R-19 (Low). Some acceptance criteria and gates cannot be checked meaningfully

- **Where:** PLAN WP-23 acceptance 2 (l.347), WP-09 acceptance 9; EVALS 3 (mean reciprocal rank gate "at least 0.75 (confirm after the first run)"); WP-23 Build "fixes for gaps found".
- **Evidence:**
  - "every MVP requirement ID appears in at least one test's name, marker, or docstring" can be satisfied by labels and does not test the requirement.
  - The f-string scan is a heuristic that can fail on correct code or miss real string building.
  - A gate that is "confirmed after the first run" has no value a test can check today.
- **Smallest fix:**
  - Replace WP-23 acceptance 2 with the section 16 table mapped to named tests that are reviewed in the pull request.
  - Make WP-09 acceptance 9 an import or AST check that `sqlglot.exp` is the only way SQL is produced.
  - Fix the MRR gate value now at 0.75 and allow lowering it only through a reviewed change to `gates.toml`.

#### R-20 (Low). An example synonym makes the resolver misread G09

- **Where:** `semantic/schema/examples/lending/entities/product.yaml` l.35; `ARCHITECTURE.md` 8.4 step 4; EVALS G09.
- **Evidence:**
  - "syariah" is a **dimension** synonym of `financing_scheme` and also the description of the value `sharia`.
  - The resolver matches dimension synonyms before values, so "... untuk produk syariah ..." produces a `financing_scheme` grouping as well as the filter. The expected request is the filter only.
- **Smallest fix:** remove "syariah" from the dimension's synonyms (keep it in the value description), or give `values` their own `synonyms` field and say so in 8.4.

## Appendix A. Requirement to architecture to work package

Built mechanically from `REQUIREMENTS.md` (51 IDs, 39 MVP including WEB-5 basic), `ARCHITECTURE.md` section 16, the `PLAN.md` table and `docs/progress.json`. The work-package sets agree across all three for every ID. The "Review note" column records where this review found the home weak.

| ID | Architecture section | Work package(s) | Review note |
|---|---|---|---|
| CON-1 | 6.1 | WP-06 | |
| CON-2 | 6.1, DR-2 | WP-21 | Integration with a real server is manual only (acceptance 4). |
| CON-3 | 6.2 | WP-06 | |
| CON-4 | 6.3, 12, 13 `/account` | WP-11 | Credentials reach the agent (R-3). |
| SEM-1 | 5.1, schema | WP-03 | |
| SEM-2 | 5.1 | WP-03, WP-04 | Synonyms not required (R-16). |
| SEM-3 | 5.2, 10 | WP-03, WP-18 | |
| SEM-4 | 5.3 R01-R04, 6.4, 13 `/admin/catalog` | WP-06, WP-19 | |
| SRC-1 | 8.2 | WP-07 | |
| SRC-2 | 8.1, 8.2 | WP-07 | Multilingual model quality unproven (spike stand-in vectors). |
| SRC-3 | 8.2 | WP-07 | Catalog documents are labelled approved (R-14). |
| SRC-4 | 8.1 | WP-07 | |
| QRY-1 | 7.1-7.5 | WP-08, WP-09 | |
| QRY-2 | 7.6 | WP-10 | Per-person rows via masked keys (R-4); final-check gaps (R-9). |
| QRY-3 | 7.8 | WP-09 | |
| QRY-4 | 4.2, 6.3, 12 | WP-11, WP-12 | R-3. |
| MCP-1 | 12 | WP-13 | stdio identity undefined (R-2). |
| MCP-2 | 12 | WP-13 | "Nothing matches" guidance relies on the band (R-5). |
| MCP-3 | 12 | WP-22 | |
| WEB-1 | 13 | WP-17 | |
| WEB-2 | 13 | WP-19 | |
| WEB-3 | 10, 11, 13 | WP-18, WP-19 | Bypass via git (R-1); approval rules (R-6, R-18). |
| WEB-4 | 8.4, 13 `/bench` | WP-20 | |
| WEB-5 (basic) | 11 | WP-12, WP-16 | |
| GOV-1 | 10, 14, 13 `/audit` | WP-18, WP-19 | External commits logged but not governed (R-1). |
| GOV-2 | 14, 13 `/logs/tool-calls` | WP-12, WP-19 | Redaction incomplete (R-12). |
| EVL-1 | EVALS 2 | WP-14 | |
| EVL-2 | EVALS 3 | WP-15 | |
| EVL-3 | EVALS 4 | WP-15 | |
| EVL-4 | EVALS 5 | WP-22 | Optional at release (R-11); exposure scan blind to masked derivatives (R-4). |
| EVL-5 | EVALS 6, ARCH 15 | WP-15, WP-22 | |
| DAT-1 | SAMPLE_DOMAIN 3 | WP-02 | |
| DAT-2 | SAMPLE_DOMAIN 4 | WP-02 | |
| DAT-3 | SAMPLE_DOMAIN 5 | WP-04 | Acceptance contradictions (R-8). |
| NFR-1 | 9 | WP-05, WP-23 | |
| NFR-2 | 8.1, 8.2, EVALS 3 | WP-07, WP-15, WP-23 | Real-model latency unmeasured. |
| NFR-3 | 9 | WP-07, WP-23 | |
| NFR-4 | 9 | WP-01 | Engine secrets configured in agent clients (R-3). |
| NFR-5 | 15 | WP-01, WP-23, all | |

Not MVP (correctly left out, with a stated home): CON-5, CON-6, CON-7, SEM-5, SRC-5 (but see R-17), QRY-5, MCP-4, MCP-5, WEB-5 SSO, WEB-6, WEB-7, GOV-3, GOV-4.

Dependency graph: 23 packages, no cycles, no edge to a later-numbered package. Titles, requirements and dependencies are identical between `PLAN.md` and `progress.json`. The edges that are missing are listed in R-13.

## Appendix B. Probe scripts and outputs

The probe scripts and their outputs are committed in `docs/reviews/design-probes/` (outputs in `output/`). The spike reruns used the committed spike scripts unchanged. Run a probe from the repository root, for example:

```
uv run --no-project --with sqlglot --with duckdb --with pyyaml python docs/reviews/design-probes/reident_probe.py
uv run --no-project --with jsonschema --with pyyaml python docs/reviews/design-probes/validate_examples.py
uv run --no-project -p 3.12 --with pgserver --with "psycopg[binary]" --with pyyaml --with numpy python docs/reviews/design-probes/confidence_probe.py
```

| What | Script | Output |
|---|---|---|
| Compiler spike rerun | `spikes/compiler/run_spike.py` | identical to `spikes/compiler/output.txt` |
| Search spike rerun | `spikes/search/run_spike.py` | identical to `spikes/search/output.txt` except latency |
| Schema validation | `validate_examples.py` | `output/schema_validation_output.txt` |
| Schema negative probes | `schema_negative.py` | `output/schema_negative_output.txt` |
| Confidence band on spike data | `confidence_probe.py` | `output/confidence_probe_output.txt` |
| Exact search, untyped vector | `exact_probe.py` | `output/exact_probe_output.txt` |
| Per-person rows via masked dimensions | `reident_probe.py` | `output/reident_probe_output.txt` |
| Plan, progress and section 16 cross-check | `plan_check.py` | `output/plan_check_output.txt` |
