# Build Plan

Status: design output (2026-11-11). Progress is tracked in `docs/progress.json`.

## How to use this plan

- One work package (WP) is one branch (`<area>/<short-description>`) and one pull request. Reference the requirement IDs in commits and in the pull request.
- Before starting a package, read `CLAUDE.md`, the sections of `docs/ARCHITECTURE.md` named in the package, and every file in `docs/notes/` (they are short).
- A package is done when every acceptance criterion is covered by an automated test that passes in CI, unless the criterion starts with "Manual". For a manual criterion, the pull request states who checked it and what they saw. Then set its status in `docs/progress.json` to `done`.
- Acceptance criteria are the minimum. Do not add features beyond the package's scope. If a criterion looks wrong or cannot be met, stop and say so in the pull request; do not weaken it.
- **Where tests run (D-15).** A coding session may have no Docker, so no Trino and no dev stack.
  - Run the unit tier locally. Compiler tests execute the generated SQL on DuckDB (`docs/ARCHITECTURE.md` section 15). A DuckDB pass shows the logic is right; it does not prove the SQL works on Trino.
  - Run the integration tier locally only if a PostgreSQL with pgvector is available (`MAKNA_TEST_DATABASE_URL`). Otherwise the CI integration job is the proof.
  - Anything that needs Trino or the dev stack (the engine tier, criteria marked "CI stack job", the retrieval and compile evals) is proven only when the CI stack job on GitHub Actions passes for the pull request. Write those tests, push, and read the CI result. Never mark such a criterion met because a DuckDB run passed, and never set the package to `done` while the stack job is red or has not run. Until then the package is `in_review`, and the pull request lists the criteria that are waiting for CI.
- Statuses in `docs/progress.json`: `not_started`, `in_progress`, `in_review`, `done`, `blocked`.
- Suggested models: **Sonnet 5.5** for most packages; **Haiku 5.5** where the work is well specified and high in volume. The orchestrator may split a package between them (for example Sonnet writes the generator's core logic and Haiku the fixtures).

## Order and milestones

The order gets a thin slice of the whole system running as early as possible: by the end of milestone 1 an agent can search, read a definition, compile, and run a query on Trino through MCP, and the evals measure it. The web app comes after the core is measured.

```
M0 Foundations        WP-01 → WP-02, WP-03 → WP-04, WP-05
M1 Agent path         WP-06 → WP-07; WP-08 → WP-09 → WP-10 → WP-11; WP-12 → WP-13; WP-14 (after WP-08) → WP-15
   -- Fable review 1: core complete (model, search, compiler) --
M2 People path        WP-16 → WP-17; WP-18 → WP-19; WP-20; WP-21; WP-22; WP-23
   -- Fable review 2: before the MVP release --
```

Packages that can run in parallel once their dependencies are done: WP-02 with WP-03; WP-04 with WP-05; WP-06 with WP-08; WP-07 with WP-09 and WP-10; WP-14 with WP-12 and WP-13; WP-17, WP-18, WP-21 with each other.

| ID | Title | Requirements | Depends on | Model |
|---|---|---|---|---|
| WP-01 | Project scaffold and CI | NFR-4, NFR-5 | — | Sonnet 5.5 |
| WP-02 | Synthetic data generator | DAT-1, DAT-2 | WP-01 | Sonnet 5.5 |
| WP-03 | Definition models, loader, validator | SEM-1, SEM-2, SEM-3 | WP-01 | Sonnet 5.5 |
| WP-04 | Sample definitions for the lending domain | DAT-3, SEM-2 | WP-03 | Haiku 5.5 |
| WP-05 | Development stack and database migrations | NFR-1 | WP-02 | Sonnet 5.5 |
| WP-06 | Catalog model, file connector, sync, reference check | CON-1, CON-3, SEM-4 | WP-03, WP-04, WP-05 | Sonnet 5.5 |
| WP-07 | Indexer and hybrid search | SRC-1, SRC-2, SRC-3, SRC-4, NFR-2, NFR-3 | WP-04, WP-06 | Sonnet 5.5 |
| WP-08 | Compiler: model snapshot, join graph, request planning | QRY-1 | WP-03, WP-04 | Sonnet 5.5 |
| WP-09 | Compiler: SQL generation and explanation | QRY-1, QRY-3 | WP-02, WP-08 | Sonnet 5.5 |
| WP-10 | Compiler: sensitivity, masking, refusals | QRY-2 | WP-09 | Sonnet 5.5 |
| WP-11 | Trino connector and metrics on the real engine | CON-4, QRY-4 | WP-05, WP-10 | Sonnet 5.5 |
| WP-12 | Service layer, users, roles, tokens, tool-call log | QRY-4, WEB-5, GOV-2 | WP-07, WP-11 | Sonnet 5.5 |
| WP-13 | MCP server | MCP-1, MCP-2 | WP-12 | Sonnet 5.5 |
| WP-14 | Golden question set | EVL-1 | WP-04, WP-05, WP-08 | Sonnet 5.5 (first 20 and review), Haiku 5.5 (drafting the rest) |
| WP-15 | Eval runner: retrieval and compile | EVL-2, EVL-3, EVL-5, NFR-2 | WP-12, WP-14 | Sonnet 5.5 |
| WP-16 | Web shell: layout, sign-in, sessions | WEB-5 | WP-12 | Sonnet 5.5 |
| WP-17 | Discovery pages | WEB-1 | WP-16 | Sonnet 5.5 (pages), Haiku 5.5 (templates and page tests) |
| WP-18 | Definition store write path and lifecycle | SEM-3, WEB-3, GOV-1 | WP-12 | Sonnet 5.5 |
| WP-19 | Editor, review queue, and curator tools | WEB-2, WEB-3, SEM-4, GOV-1, GOV-2 | WP-16, WP-18 | Sonnet 5.5 |
| WP-20 | Resolver and test bench | WEB-4 | WP-16, WP-15 | Sonnet 5.5 |
| WP-21 | OpenMetadata connector | CON-2 | WP-06 | Sonnet 5.5 |
| WP-22 | Agent comparison eval and MCP client compatibility | EVL-4, EVL-5, MCP-3 | WP-13, WP-15 | Sonnet 5.5 |
| WP-23 | Hardening and release check | NFR-1, NFR-2, NFR-3, NFR-5 | all others | Sonnet 5.5 |

---

## M0 Foundations

### WP-01 Project scaffold and CI

- **Read**: `docs/ARCHITECTURE.md` sections 2, 3, 9 (configuration), 15.
- **Build**: `pyproject.toml` (uv, distribution `makna-sl`, package `makna`, Python 3.12 or newer, no publishing setup); the empty packages of section 2; `makna.config` (settings from environment variables and `config/makna.toml`); the `makna` CLI entry point with `--help`; ruff, mypy (strict for `makna.model` and `makna.compiler`), pytest with the three tiers as markers; a GitHub Actions workflow running lint, types, and unit tests; `deploy/.env.example`; the "Commands" section of `CLAUDE.md` filled in.
- **Acceptance**
  1. `uv sync && uv run pytest -m unit` passes on a fresh clone.
  2. `uv run ruff check .` and `uv run mypy` pass.
  3. A unit test fails if any module in `makna.mcp`, `makna.web`, or `makna.cli` imports from a package other than `makna.services`, `makna.model`, and `makna.config`, or if `makna.compiler` imports a database or network library (the dependency rule of section 2).
  4. A unit test fails if `deploy/.env.example` contains a non-empty value, and if any tracked file is named `.env`.
  5. Loading settings with a required variable missing raises an error that names the variable.

### WP-02 Synthetic data generator

- **Read**: `docs/SAMPLE_DOMAIN.md` sections 2 to 4 and 6.1.
- **Build**: `data/generator/` with a command `uv run python -m data.generator --out data/generated --seed 20261111 --scale 1.0` that writes one Parquet file per table, and a table specification module (names, columns, types, true meanings, catalog descriptions, personal flags) that WP-05 and WP-06 reuse. Column names are exactly those of section 3, including the abbreviated, mixed-language names of `loan_applications` and `collection_actions` (Q11).
- **Acceptance**
  1. Two runs with the same seed give byte-identical files (compare SHA-256); a different seed gives different files.
  2. All nine tables exist with exactly the columns and types in section 3. Row counts at scale 1.0 are within 25% of the targets in section 2.
  3. Every check in the "Check on the generated data" column of section 4 (Q1 to Q10) is a passing test.
  4. Referential integrity: every foreign key in section 3.10 resolves; every primary key is unique.
  5. `--scale 0.05` finishes in under 20 seconds (used by tests).
  6. The table specification's catalog descriptions equal section 6.1 exactly: a test walks the table of section 6.1, and asserts that every object not listed there has a non-empty English description (D-14).

### WP-03 Definition models, loader, validator

- **Read**: `docs/ARCHITECTURE.md` section 5; `semantic/schema/makna.schema.json` and the examples.
- **Build**: Pydantic models in `makna.model.definitions` matching the JSON Schema; `makna.definitions.loader` (read a repository directory into a `DefinitionSet`, official and proposal files); `makna.definitions.validate` implementing V01 to V17 (reference issues R01 to R04 belong to WP-06); CLI `makna validate <dir>` printing one line per problem with file, line where known, code, and message, and exiting 1 on any error.
- **Acceptance**
  1. Every file under `semantic/schema/examples/` loads and validates with zero errors.
  2. For each rule V01 to V17 there is at least one fixture that breaks only that rule, and the validator reports exactly that code.
  3. A test proves the Pydantic models and the JSON Schema agree: every example round-trips (load, dump, validate against the schema), and a property the schema forbids (an unknown key, a ratio with `measure`) is rejected by both.
  4. `makna validate semantic/schema/examples` exits 0; on a directory with one broken fixture it exits 1 and prints the rule code.
  5. Loading never executes anything from YAML (uses the safe loader; a test feeds a YAML tag payload and expects a clean error).

### WP-04 Sample definitions for the lending domain

- **Read**: `docs/SAMPLE_DOMAIN.md` sections 3 and 5; the example files.
- **Build**: `semantic/definitions/lending/`: the domain file, 9 entity files (the `application` and `collection_action` entities read the abbreviated physical columns of `docs/SAMPLE_DOMAIN.md` sections 3.4 and 3.8 but give every dimension a clean English name, a full description, and synonyms), 23 approved metric files, `write_off_amount.draft.yaml`, the deprecated `npl_ratio.yaml`, and at least 8 glossary terms. Copy the seven starred example metrics unchanged; extend the example entities.
- **Acceptance**
  1. `makna validate semantic/definitions` exits 0.
  2. A test asserts the exact set of 23 approved metric names in section 5, their types, and (for simple metrics) their measure's entity and time dimension as listed in the tables.
  3. Every metric and every entity has at least one English and one Indonesian synonym; every metric has at least two example questions; every dimension and measure has a description.
  4. Every metric whose table row says "booking" or "servicing" pins `branch` to a path ending in `contract.booking_branch` or `contract.servicing_branch` respectively.
  5. The five personal columns of `customers` are each read by a dimension with `sensitivity: personal`; `full_name`, `national_id`, and `email` have no masking rule.
  6. The files under `semantic/schema/examples/lending/metrics/` are identical to the files of the same name in `semantic/definitions/lending/metrics/`.

### WP-05 Development stack and database migrations

- **Read**: `docs/ARCHITECTURE.md` sections 6.4 and 9; DR-11.
- **Build**: `deploy/docker-compose.yml` with `postgres`, `trino`, `init`, and `app`; a minimal `makna serve` (a FastAPI application with only `/healthz`, extended by WP-12 and WP-16); an `init` entry point that runs an ordered list of idempotent steps, to which later packages add their step (WP-06 catalog sync, WP-07 index build, WP-12 demo users, WP-18 definitions repository); Trino catalog configuration; a loader that creates `lending.core` tables from the WP-02 table specification and loads the Parquet files; Alembic migrations for all tables in section 6.4; separate PostgreSQL roles for the `makna` and `lending` databases (section 9); a CI job that starts the stack. Pin the Trino and PostgreSQL image versions and record them in `deploy/README.md`.
- **Acceptance**
  1. `docker compose -f deploy/docker-compose.yml up -d --wait` ends with all services healthy on a clean machine (CI job).
  2. Through Trino, `SELECT count(*) FROM lending.core.<table>` equals the row count of the generated file, for all nine tables.
  3. `alembic upgrade head` on an empty database creates every table of section 6.4; `alembic downgrade base` removes them; the `vector` extension is present.
  4. Running `init` a second time changes nothing and exits 0.
  5. No secret has a value in any committed file; the compose file reads them from the environment with development defaults only for the local PostgreSQL passwords.
  6. The `makna` database role cannot connect to the `lending` database, and Trino's role can only read it (CI stack job). Trino's port is published on `127.0.0.1` only.

## M1 Agent path

### WP-06 Catalog model, file connector, sync, reference check

- **Read**: `docs/ARCHITECTURE.md` sections 5.3 (reference issues), 6.1, 6.2, 6.4, 7.6 (tag mapping).
- **Build**: `makna.model.catalog`; `makna.catalog.base`; `makna.catalog.file` (format `makna-catalog/v1`); `data/catalog/lending.catalog.yaml` generated from the WP-02 table specification, with the catalog descriptions of `docs/SAMPLE_DOMAIN.md` section 6.1 (deliberately poor in places, D-14), the `PII.Sensitive` tag on the five personal columns, and the glossary terms; `makna.catalog.sync` (full sync into the catalog tables, `sync_run` summary); `makna.catalog.references` (R01 to R04, writing `reference_issue`); CLI `makna catalog sync` and `makna catalog issues`.
- **Acceptance**
  1. Syncing the sample export creates 9 datasets and the exact column count of `docs/SAMPLE_DOMAIN.md` section 3; a second sync reports zero changes in `sync_run.summary`.
  2. After removing a column from a copy of the export and syncing, `sync_run.summary` counts one removed column, and a reference issue R02 is open for each definition part that reads it; restoring the column and syncing closes the issue (`closed_at` set).
  3. Each of R01 to R04 has a test that opens exactly that issue.
  4. A column tagged `PII.Sensitive` gets `derived_sensitivity = personal`; a dimension declared `internal` that reads it gets issue R03.
  5. An export file that does not match the format is rejected with an error naming the field.
  6. With the sample definitions and the sample export, there are zero open reference issues.
  7. Q11: the committed export carries the section 6.1 descriptions exactly (empty, vague, and Indonesian ones included), and regenerating it from the table specification gives a byte-identical file.

### WP-07 Indexer and hybrid search

- **Read**: `docs/ARCHITECTURE.md` sections 4.1, 5.4, 6.4, 8.1 to 8.3; `spikes/search/README.md`; `docs/notes/search-scores-cannot-say-no-match.md`.
- **Build**: `makna.index.indexer` (`build` from a definitions directory and the catalog tables; `index_object` for one object; of the derived facts of section 5.4, compute inputs and the formula line here; available dimensions are added by WP-12, which connects the indexer to the planner of WP-08); `makna.search.embedding` with the `fastembed`, `ollama`, `openai_compatible`, and `hash` providers; `makna.search.query`; `makna.search.hybrid`; `makna.search.confidence`; CLI `makna index build` and `makna search "<text>"`.
- **Acceptance** (integration tier, `hash` provider unless stated)
  1. After `makna index build` on the sample definitions and catalog, `search_document` holds one document for each case listed under "What becomes a search document" in section 6.4 (assert counts per kind).
  2. The queries "NPF", "kredit macet", and "non-performing financing ratio" each return `metric:npf_ratio` in the first 3 results (SRC-2).
  3. `metric:npl_ratio` (deprecated) is absent by default and present with `include_deprecated`; `metric:write_off_amount` (draft) is absent for an agent or viewer and present for a curator, ranked below an approved metric that matches the same query equally (SRC-3; build the tie with a fixture).
  4. With all status and kind weights set to 1: when the keyword weight is 0 the result order equals the vector order, and when the vector weight is 0 it equals the keyword order (fusion is wired correctly).
  5. Each provider passes a contract test (dimension matches, same text gives the same vector); `ollama` and `openai_compatible` are tested against a fake HTTP server. No test downloads a model.
  6. The confidence band is `low` for "website conversion funnel" and not `low` for "kredit macet".
  7. Changing one metric's synonyms and calling `index_object` changes only that object's rows (other rows keep their `text_hash` and update time).
  8. Engine tier, real default model: the 95th percentile of 200 searches is under 500 ms (NFR-2), and "pembiayaan bermasalah" returns `metric:npf_ratio` or `metric:npf_outstanding_principal` first.
  9. With network access disabled after the image is built, `makna index build` and a search succeed (NFR-3; run in the CI stack job).
  10. A `column` document for a column with derived sensitivity `personal` (for example `customers.national_id`) is never returned to a viewer or agent caller, even with `kinds: [column]`, and is returned to a curator.

### WP-08 Compiler: model snapshot, join graph, request planning

- **Read**: `docs/ARCHITECTURE.md` sections 5.4, 7.1 to 7.4, 7.7; `spikes/compiler/`.
- **Build**: `makna.model.request` (request, response, error types); `makna.compiler.graph` (entities and relationships, path search with the rule of section 7.3); `makna.compiler.plan` (expand inputs, resolve paths, compute available dimensions, validate grain, filters, time range, and limit; no SQL yet); a pure function `available_dimensions(model, metric)` that WP-12 calls from the indexer.
- **Acceptance** (unit tier; no database)
  1. On the sample definitions: `contract` to `customer` resolves to the one-step path; `contract` to `branch` without a pin raises `AMBIGUOUS_JOIN_PATH` listing both one-step paths; with a pin it returns the pinned path; `branch` to `contract` raises `NO_JOIN_PATH`.
  2. `npf_ratio` lists `branch.region_name` and `product.product_category` as available and does not list `payment.payment_channel`. A fixture ratio whose inputs pin different branch links does not list any `branch` dimension.
  3. Each error code of section 7.7 that planning can raise (`UNKNOWN_METRIC`, `NOT_APPROVED`, `DEPRECATED`, `BROKEN_REFERENCE`, `UNKNOWN_DIMENSION`, `DIMENSION_NOT_AVAILABLE`, `INVALID_GRAIN`, `INVALID_FILTER`, `INVALID_TIME_RANGE`, `LIMIT_EXCEEDED`) has a test, and each error's details contain what the table says.
  4. Planning `write_off_amount` raises `NOT_APPROVED`; planning `npl_ratio` raises `DEPRECATED` with `replaced_by`.
  5. Planning is a pure function: the same inputs give an equal plan object, and the module imports no I/O library.

### WP-09 Compiler: SQL generation and explanation

- **Read**: `docs/ARCHITECTURE.md` sections 7.2, 7.5, 7.8; `spikes/compiler/README.md`; `docs/notes/never-join-fact-tables.md`.
- **Build**: `makna.compiler.sqlgen` (plan to sqlglot tree to SQL text for a dialect) and `makna.compiler.explain`; a test fixture that loads WP-02 data at scale 0.05 into DuckDB under database `lending`, schema `core`; a DuckDB-backed test helper that prints the tree for DuckDB and runs it.
- **Acceptance** (unit tier)
  1. The five cases of `spikes/compiler/run_spike.py` are re-implemented as tests against the product compiler and the WP-02 fixture data: each compiled result equals a hand-written reference query's result (reference SQL kept in the test files, written without the compiler).
  2. For `collection_rate`, the generated tree contains no join between `payments` and `installment_schedules` (assert on the tree), and the result differs from the naive joined query on the fixture data.
  3. For a balance metric over a six-month range with no grain, only one snapshot date contributes (assert by comparing with a reference filtered to the last month end); with grain `quarter`, exactly the last month end of each quarter contributes.
  4. Result columns and their order follow section 7.2 for a simple metric and for a ratio.
  5. A derived metric fixture (`a - b`) and a ratio with a zero denominator (result NULL, no error) pass.
  6. A filter value containing a quote and SQL keywords returns zero rows and the printed SQL contains it only as one string literal (assert on the tree).
  7. The SQL printed for Trino re-parses with sqlglot's Trino dialect for every approved sample metric at its default grain.
  8. The explanation for `collection_rate` by region mentions, in order: the formula, each input's time dimension and time range, each definition filter, and the join path to branch; a balance metric adds the balance warning.
  9. No function in `makna.compiler` builds SQL with string formatting or concatenation: a test scans the package's source for f-strings and `%`/`.format` on strings containing SQL keywords and fails if any are found.

### WP-10 Compiler: sensitivity, masking, refusals

- **Read**: `docs/ARCHITECTURE.md` section 7.6; DR-5.
- **Build**: effective sensitivity (YAML level and catalog-derived level, the latter passed into the compiler as data so the compiler stays free of database access); masking expressions for the four methods; the refusal rules; `makna.compiler.governance.verify(tree, plan)`.
- **Acceptance** (unit tier)
  1. Selecting `customer.birth_year` returns only first-of-January dates; a fixture numeric dimension with `bucket` width 10 returns multiples of 10; `redact` returns `***`.
  2. Selecting `customer.full_name`, `customer.national_id`, `customer.email`, or `customer.phone_number` raises `PERSONAL_DATA_REFUSED`.
  3. A filter on any personal dimension, masked or not, raises `PERSONAL_DATA_REFUSED`.
  4. A dimension declared `internal` whose column has `derived_sensitivity = personal` is treated as personal (refused without masking).
  5. `verify` raises when given a hand-built tree that selects a personal column bare or inside `MIN` or `MAX`, and passes when the same column is inside a mask or inside `COUNT` or `COUNT(DISTINCT ...)`.
  6. Property test: for at least 500 random valid requests over the sample model (random metric, dimensions drawn from all dimensions including personal ones, random filters), every request either raises a section 7.7 error or yields SQL that passes `verify`, whose DuckDB result contains no raw value of any personal column of the fixture `customers` table, and, when a personal dimension is selected, no group built from fewer than `min_group_size` customers.
  7. Minimum group size: on the fixture data, `npf_outstanding_principal` by `customer.birth_year`, `customer.province`, and `branch.region_name` returns no group built from fewer than `min_group_size` customers (checked against a reference count query), the warning states how many groups were left out, and the same metric by `branch.region_name` alone is not suppressed. A ratio drops a group when either input has it from fewer than the minimum.

### WP-11 Trino connector and metrics on the real engine

- **Read**: `docs/ARCHITECTURE.md` sections 6.3, 9; `docs/notes/spike-environment-limits.md`.
- **Build**: `makna.engine.base`; `makna.engine.trino` (`list_tables`, `execute` with user and optional password, row limit, timeout, clear errors); CLI `makna catalog compare` (catalog copy versus engine introspection).
- **Acceptance** (engine tier)
  1. `list_tables` for `lending.core` returns the nine tables with their columns.
  2. `execute` returns columns and rows; with `max_rows=5` on a larger result it returns 5 rows and `truncated = true`; a query exceeding `timeout_s` raises `ENGINE_TIMEOUT`; invalid SQL raises `ENGINE_ERROR` carrying Trino's message.
  3. The five spike cases, compiled by the product compiler, run on Trino and equal their reference queries run on Trino. Manual: record in `deploy/README.md` that the open points in `spikes/compiler/README.md` are confirmed, or what had to change.
  4. Every approved sample metric compiles and runs on Trino with no dimensions and at its default grain, returning at least one row.
  5. The query runs under the user given in the credentials: `SELECT current_user` through `execute` returns that user. A unit test with a mocked client asserts that a password, when given, is sent as basic authentication and is never written to a log record.
  6. `makna catalog compare` reports no differences on the dev stack, and reports a missing column after one is dropped from a scratch copy of a table.

### WP-12 Service layer, users, roles, tokens, tool-call log

- **Read**: `docs/ARCHITECTURE.md` sections 4, 11, 14.
- **Build**: `makna.services`: `search`, `get_definition`, `list_related`, `compile_query`, `run_query`, each taking a `Caller` (user or token, role, teams, channel); the indexer step that stores each metric's available dimensions (using `available_dimensions` from WP-08) and each dimension's catalog-derived sensitivity; the in-memory model snapshot refreshed on `definitions_version`; user and token management (`makna user create`, `makna token create`, with the owner's engine credentials stored encrypted for a token with `can_run`); the permission checks of section 11 as one module; redaction and `tool_call_log` writes; `makna serve` (FastAPI app with `/healthz`).
- **Acceptance** (integration tier)
  1. A table-driven test covers every cell of the permission matrix in section 11 for the read and run permissions (write permissions are tested in WP-18).
  2. `search` as an agent token or viewer never returns a draft; as a curator it can.
  3. `get_definition` for a metric returns the fields shown in section 12, including available dimensions with their path label and the unavailable personal dimensions with a reason.
  4. `list_related("metric:npf_ratio")` includes its two inputs (`input_of`) and the glossary term `npf` (`explains`).
  5. `run_query` raises `RUN_DISABLED` when `MAKNA_RUN_ENABLED` is false, `ENGINE_CREDENTIALS_REQUIRED` for a token with no stored credentials, and with a fake engine returns rows, the fake engine receiving the token owner's stored credentials. The `api_token` row holds no plaintext password (assert on the raw column), and decrypting with a different `MAKNA_CREDENTIALS_KEY` fails.
  6. Every service call writes one `tool_call_log` row, including failed and refused calls with `status` and `error_code`. A call whose query text contains an email address, a 16-digit number, and a phone number stores none of them. No row contains result row values (assert on a run that returns known values).
  7. A token is shown once at creation, stored only as a hash, rejected after revocation, and `last_used_at` is updated.
  8. After changing an official file and calling `index_object`, the next `get_definition` in the same process returns the new content with no restart.

### WP-13 MCP server

- **Read**: `docs/ARCHITECTURE.md` section 12; `docs/notes/mcp-sdk-2-renamed-fastmcp.md`.
- **Build**: `makna.mcp.server` with the five tools, the server instructions, tool descriptions exactly as in section 12 (improve wording only with a note in the pull request), Streamable HTTP mounted at `/mcp` with bearer-token authentication, and `makna mcp`, a stdio-to-HTTP bridge holding only `MAKNA_URL` and `MAKNA_TOKEN` (section 12).
- **Acceptance** (integration tier, using the SDK's client in process and over HTTP)
  1. Listing tools returns the four always-on tools, plus `makna_run_query` only when running is enabled and the token has `can_run`. Every tool has `readOnlyHint` set. A snapshot test compares each tool description and the server instructions with a reviewed text file in the test directory, so a wording change is always a visible, reviewed change; the first snapshot is the text of section 12 (MCP-2).
  2. A full flow passes: search "kredit macet per wilayah", get the top metric's definition, compile a request using a dimension from that definition, and (with the fake engine) run it.
  3. `makna_search` output for each of the 20 question texts in `docs/EVALS.md` section 2.5 (copied into the test) is at most 2,500 characters at the default limit; `makna_get_definition` in `full` is at most 6,000 characters for every approved sample metric.
  4. A compile error is returned as a tool error whose JSON has `code`, `message`, and `details`; for `DIMENSION_NOT_AVAILABLE` the details list available dimensions.
  5. HTTP without a token, with a revoked token, and with a malformed token is rejected with 401.
  6. No tool accepts SQL text: the input schema of every tool has no property named `sql` or `query_sql`, and a test asserts this.
  7. The stdio bridge refuses to start without `MAKNA_TOKEN`, starts and completes a search with no `MAKNA_DATABASE_URL` in its environment, and its calls are logged with the token's user. No tool input schema has a property for engine credentials, `X-Makna-Engine-*` headers are ignored, and `makna_run_query` reaches the fake engine with the token's stored credentials, which appear in no log row and no response.

### WP-14 Golden question set

- **Read**: `docs/EVALS.md` sections 1 and 2; `docs/SAMPLE_DOMAIN.md`; `docs/notes/never-join-fact-tables.md`.
- **Build**: `evals/golden/questions.yaml` with the first 20 questions unchanged and at least 30 more following the composition table; reference SQL for every `answer` question and `clarify` candidate; `evals/golden/expected/*.csv` frozen from the dev stack; a schema for the questions file. Reference SQL must be written from the plain-language definitions, without running the compiler.
- **Acceptance**
  1. The questions file validates against its schema; there are at least 50 questions; the counts per group meet or exceed the composition table of section 2.4.
  2. Every approved metric is the expected metric of at least one `answer` question.
  3. Every reference SQL runs on the dev stack's Trino and returns between 1 and 200 rows; re-running gives the committed CSV (engine tier).
  4. No question text contains the `name` of its expected metric.
  5. Every `expected.request` and every candidate request passes planning (WP-08) with no error; every `no_metric` question with reason `dimension_not_available` or `not_approved` has a request that fails planning with the matching code.
  6. Manual: a second reviewer (person or a different model) has checked each reference SQL against the definition; the pull request lists the reviewer and any disagreements resolved.

### WP-15 Eval runner: retrieval and compile

- **Read**: `docs/EVALS.md` sections 3, 4, 6.
- **Build**: `evals/` runner with the `makna-eval` command (`all`, `retrieval`, `compile`, `freeze`), reports in Markdown and JSON, gates in `evals/gates.toml`, the CI wiring, and one tuning pass of the search weights and confidence thresholds with the real default model.
- **Acceptance**
  1. `uv run makna-eval all` on the dev stack writes `report.md` and `report.json` containing every score in the tables of sections 3 and 4, including keyword-only, vector-only, and fused retrieval scores and the per-language split.
  2. The exit code is 1 when a gate fails: a test lowers a gate fixture and asserts the failure, and breaks one reference CSV and asserts the compile gate fails naming the question.
  3. The result comparison function has unit tests for: row order differences (equal), numeric tolerance (equal inside, different outside), an extra column in the compiled result (equal), a missing row (different).
  4. Compile part B covers every approved metric and every available non-personal dimension (assert the count of executed requests equals the count derived from the index).
  5. On the dev stack with the real default embedding model: recall at 3 is at least 0.90 and compile correctness is 100%. If recall is below the gate after tuning, do not lower the gate: report the failing questions and the per-leg scores in the pull request and mark the package `blocked` for the owner's decision on the embedding model (DR-9).
  6. Manual: the tuned weights and thresholds are committed in `config/makna.toml`, and the pull request shows the scores before and after tuning.
  7. The CI stack job runs `makna-eval all` and uploads the report.

**Fable review 1 (core complete)** follows WP-15.

## M2 People path

### WP-16 Web shell: layout, sign-in, sessions

- **Read**: `docs/ARCHITECTURE.md` sections 11, 13; DR-8.
- **Build**: base template, navigation, vendored HTMX, Pico CSS, and Ace under `makna/web/static/vendor/` with a `VENDORED.md` listing versions, licences, and source URLs; `/login`, `/logout`, `/account` (own tokens, with engine credentials for a token that may run queries; engine credentials for the web session); `/admin/users` and `/admin/tokens`; session cookie; CSRF protection; a role-aware navigation; error pages.
- **Acceptance** (integration tier, FastAPI test client)
  1. Every route except `/login`, `/healthz`, and static files redirects to `/login` when signed out.
  2. Signing in with a wrong password fails with the same message as an unknown email; five failures in a minute for one email are followed by a refusal (basic throttling).
  3. A POST without a valid CSRF token is rejected with 403.
  4. The session cookie is `HttpOnly` and `SameSite=Lax`, and `Secure` when not in development mode.
  5. Engine credentials saved on `/account` are available to a later request in the same session, are absent from the cookie and the database (assert by inspecting both), and are gone after logout.
  6. Pages render with no request to any host other than the application (assert no absolute `http` URLs in rendered HTML or vendored CSS apart from licence comments).
  7. The repository has no `package.json`, and no build step is needed to serve the pages.
  8. An admin can create a user, change a user's role and teams, deactivate a user (who can then no longer sign in), and revoke any token on `/admin/users` and `/admin/tokens`; every other role gets 403 on these routes.

### WP-17 Discovery pages

- **Read**: `docs/ARCHITECTURE.md` section 13 (the first six routes and freshness).
- **Build**: `/`, `/search`, `/domains/<name>`, `/metrics/<name>`, `/entities/<name>`, `/glossary/<name>`; `makna freshness refresh` and the `data_freshness` display.
- **Acceptance** (integration tier)
  1. The metric page for `npf_ratio` contains, in this order in the HTML: the label, the plain-language description, the formula line, the owner team, the status and version, a freshness line, the caveats, and the example questions; SQL and YAML are not shown to a viewer.
  2. `/search?q=kredit+macet` lists NPF Ratio in the first 3 results with an "approved" badge; the HTMX partial endpoint returns only the results fragment.
  3. A viewer does not see drafts in search or on domain pages; a curator sees `write_off_amount` with a "draft" badge.
  4. A deprecated metric's page shows the deprecation reason and a link to its replacement.
  5. `/domains/lending` lists every approved object of the domain, grouped by kind.
  6. After `makna freshness refresh` on the dev stack, the page for `outstanding_principal` shows "data up to 2026-06-30"; without service credentials it shows "freshness not available" (engine tier for the first half).
  7. Each example question on a metric page links to `/bench?q=<question>`.

### WP-18 Definition store write path and lifecycle

- **Read**: `docs/ARCHITECTURE.md` sections 5.2, 10, 11; DR-4, DR-6.
- **Build**: `makna.definitions.repo.GitRepo`; the seven lifecycle actions as service functions; the advisory lock; `audit_event` intent rows and writes; external-change handling (`docs/ARCHITECTURE.md` section 10); `makna index sync`; seeding of the definitions repository in `init`.
- **Acceptance** (integration tier, with a temporary git repository)
  1. Draft to approved: `create_proposal`, `submit`, `approve` (by a different user) for a new metric ends with `<name>.yaml` at status `approved` and version 1, no proposal file, three commits whose author is the acting user and whose message carries the reason and the four trailers, and three `audit_event` rows.
  2. **No redeploy**: in one running application, a `makna_search` call that did not find the new metric before approval finds it after, and `makna_compile_query` for it succeeds, with no restart between the calls.
  3. Changing an approved metric: while the proposal is in review, `get_definition` and the compiler still serve the old version; after approval they serve the new one with `version` increased by 1.
  4. A table-driven test covers every write cell of the permission matrix: a viewer and an agent token cannot write; a curator outside the owner team cannot approve; a curator inside it can; no author of any commit on the proposal can approve it (including when another user made the last edit) unless `MAKNA_ALLOW_SELF_APPROVAL` is true; a proposal that sets `owner.team` to the proposer's team does not give that team approval rights over an existing object; a change of `owner.team` can be approved only by an approver or admin; a curator cannot create a new object owned by a team they are not in.
  5. `submit` and `approve` are refused when the proposal has a definition error; `approve` is refused when the object has an open reference issue. A missing reason is refused for every action.
  6. `reject` returns the proposal to `draft` and records the reason; `withdraw` deletes the proposal file; `deprecate` sets the status with a reason, after which search hides the object and the compiler raises `DEPRECATED`.
  7. Two approvals started at the same time produce two commits in sequence and a consistent index (run with threads; no lost update).
  8. Recovery: if the index transaction is made to fail after the commit, `makna index sync` completes it from the intent row, so the index and `audit_event` match `HEAD`; running it again changes nothing.
  9. External changes: a commit made directly with git that edits an approved metric, with or without copied `Makna-*` trailers, is logged as `external_change` with the commit's author; an agent token's `get_definition` and `compile_query` still get the previous approved version; the change is listed in review as `in_review`; approving it in the application serves it, and rejecting it restores the official file and keeps the content as a draft. A new `<name>.yaml` with `status: approved` added by a raw commit is not visible to an agent token.
  10. After the rebuildable tables (catalog copy and semantic index) are emptied, `makna index build` reproduces the same `semantic_object` rows as the incremental path (compare contents, ignoring timestamps), including a file whose latest commit is an external change.

### WP-19 Editor, review queue, and curator tools

- **Read**: `docs/ARCHITECTURE.md` sections 13 (`/edit`, `/new`, `/review`, `/audit`, `/logs/tool-calls`, `/admin/catalog`) and 14.
- **Build**: the editor page with Ace, the "Check" endpoint, templates per kind, the difference view, save and submit with a reason; the review queue with approve and reject; the audit page; the tool-call log page; the catalog admin page (sync runs, open reference issues, buttons to run a catalog sync and an index sync).
- **Acceptance** (integration tier)
  1. "Check" on YAML with an unknown key, and on YAML that breaks rule V06, returns the rule code, a message, and a line number, and writes nothing to the repository.
  2. Saving a draft and submitting create the commits of WP-18 with the signed-in user as author.
  3. The review queue lists the proposal with author, reason, difference, and validation status; the Approve button is present for a user who may approve and absent for one who may not, and a forged POST by the latter is refused with 403.
  4. After approval through the web route, the metric page shows the new version.
  5. `/new/metric` opens a template that fails validation only on fields the author must fill.
  6. `/audit` shows the events of acceptance 2 to 4, newest first, and can be filtered by object.
  7. With JavaScript disabled, the editor falls back to a plain text area and saving still works (test the form POST directly).
  8. `/logs/tool-calls` lists calls newest first with channel, tool, status, error code, and duration, can be filtered by tool and status, shows the redacted request, and is refused (403) for a viewer.
  9. `/admin/catalog` shows the last sync run's summary and every open reference issue with its object and code (create one with a fixture); the sync button is refused (403) for anyone but an admin; after a sync that fixes the fixture, the issue is no longer listed (SEM-4).

### WP-20 Resolver and test bench

- **Read**: `docs/ARCHITECTURE.md` sections 8.3, 8.4, 13 (`/bench`); `docs/EVALS.md` section 5.4 (last paragraph).
- **Build**: `makna.resolve` (rules of section 8.4; `today` is always a parameter); the `resolve` service function; `/bench`; a resolver score in `makna-eval` (informative, no gate).
- **Acceptance**
  1. Unit tests for the time grammar: each phrase listed in section 8.4, in English and Indonesian, with today = 2026-07-01, gives the expected `time_start` and `time_end`.
  2. On the first 20 golden questions with today = 2026-07-01: G16 and G17 resolve to `ambiguous` with both candidates; G18 resolves to `no_match` or to `resolved` with low confidence flagged; at least 10 of the 15 `answer` questions produce a request equal to `expected.request`. Report the exact number; do not special-case question ids.
  3. `resolve` never reads the system clock (a test freezes the clock at another date and gets the same result).
  4. Words that match nothing are returned in `unresolved_terms`.
  5. `/bench?q=...` shows the five blocks of section 13 in order; editing the request JSON and submitting recompiles; a compile error is shown with its code and details; rows are shown only when running is enabled and session credentials exist.
  6. `makna-eval all` reports the resolver's exact-request accuracy on `answer` questions.

### WP-21 OpenMetadata connector

- **Read**: `docs/ARCHITECTURE.md` section 6.1; DR-2; `docs/notes/openmetadata-sdk-license.md`.
- **Build**: `makna.catalog.openmetadata` using `httpx` against the REST API (tables with columns, tags, and owners; glossary terms; pagination; bearer token from the environment; mapping from OpenMetadata service names to engine catalog names in configuration); `deploy/docker-compose.openmetadata.yml` and a seeding script that loads the sample catalog into OpenMetadata.
- **Acceptance**
  1. Unit tests with recorded API responses (committed fixtures) cover: pagination over more than one page, a table with column tags, owners of type user and team, a glossary term with synonyms, an HTTP 401, and a timeout. Each produces the expected models or a clear `ConnectorError`.
  2. The package does not depend on `openmetadata-ingestion` (assert on the lock file).
  3. Contract test shared with the file connector: both connectors, given the same sample catalog, yield equal `CatalogDataset` and `CatalogColumn` sets apart from `catalog_id` (the OpenMetadata side uses the recorded fixtures).
  4. Manual, with the OpenMetadata stack up: `makna catalog sync --source openmetadata` creates the same 9 datasets and leaves zero open reference issues. Record the OpenMetadata version tested in `deploy/README.md`.

### WP-22 Agent comparison eval and MCP client compatibility

- **Read**: `docs/EVALS.md` sections 5 and 6; `docs/ARCHITECTURE.md` section 12 (client compatibility).
- **Build**: `evals/agent/` (the tool-calling loop, the two arms, the raw-schema tools, the scorer, the report); `makna-eval agent`; compatibility tests with two MCP clients.
- **Acceptance**
  1. The scorer has unit tests for every row of the scoring table in `docs/EVALS.md` section 5.3, using hand-written agent replies: correct answer, wrong number, unit slip (3.1 for 0.031), unparseable reply, clarification on a `clarify` question, an answer with interpretation matching one candidate, an answer on a `no_metric` question (wrong), and a refusal that still contains a personal value (wrong and counted as an exposure).
  2. The personal data scan finds a known `national_id`, phone number, name, and email from the fixture data inside a tool result and inside a final reply.
  3. With a scripted fake model (no network), `makna-eval agent` runs both arms over 5 questions and writes a report containing every number listed in section 5.4, including the paired bootstrap interval (the bootstrap function has its own test with a known answer).
  4. MCP-3: the official SDK client over stdio and `langchain-mcp-adapters` over Streamable HTTP each list the tools and complete search, get definition, and compile against a running server (integration tier, no language model involved).
  5. Manual, needs a model key: one full run of both arms, 3 runs per question, with the report committed under `evals/reports/` and the gate result of section 5.4 stated in the pull request. If no key is available, mark this criterion as waiting for the owner; the package's other criteria still apply.
  6. The CI job for the agent eval is skipped with a clear message when the key is absent.

### WP-23 Hardening and release check

- **Read**: `docs/PROJECT_BRIEF.md` ("How we know it works"); `docs/REQUIREMENTS.md`; `docs/ARCHITECTURE.md` section 16.
- **Build**: an end-to-end smoke test on the dev stack; a README quick start; an operator guide (`docs/OPERATIONS.md`: configuration, backup of operational tables and the definitions repository, building the image without internet access); fixes for gaps found.
- **Acceptance**
  1. Smoke test (CI stack job): from a clean `docker compose up`, sign in as the demo curator, create and submit a metric proposal, approve it as the demo approver, find it through `makna_search` over HTTP with a token, compile it, and run it on Trino.
  2. A test walks the table in `docs/ARCHITECTURE.md` section 16 and asserts that every MVP requirement ID appears in at least one test's name, marker, or docstring.
  3. NFR-2: the search latency gate passes in the stack job with the real embedding model.
  4. NFR-3: the stack job runs the smoke test with outbound network access blocked for the `app` container.
  5. `uv run makna-eval all` passes its gates on the final commit, and the report is attached to the release pull request.
  6. Every package in `docs/progress.json` is `done`, or listed with its reason in the release pull request.

**Fable review 2 (before the MVP release)** follows WP-23.
