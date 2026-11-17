# Evaluation Design

Status: design output (2026-11-11). Covers EVL-1 to EVL-5. Decision record: DR-10 in `docs/DECISIONS.md`.

The evals answer the four success questions in `docs/PROJECT_BRIEF.md`:

| Success criterion | Eval | Gate |
|---|---|---|
| Search returns the correct metric in the top 3 for at least 90% of the golden questions. | Retrieval (section 3) | recall at 3 of at least 0.90 |
| Every compiled query for an approved metric runs and returns the expected result. | Compile correctness (section 4) | 100% |
| An agent with the MCP server answers more accurately than the same agent with raw schema access. | Agent comparison (section 5) | Makna arm accuracy higher, with the difference reported with its interval |
| A curator takes a draft to approved and agents see it without a redeploy. | Not an eval: an integration test in WP-18. | pass |

## 1. Fixed conditions

- **Data**: the synthetic lending dataset, seed `20261111`, covering 2024-01-01 to 2026-06-30 (`docs/SAMPLE_DOMAIN.md`).
- **Today**: every eval treats **2026-07-01** as the current date. So "last month" is June 2026, "last quarter" is April to June 2026, "this year" is 2026-01-01 up to 2026-07-01, and the latest month end is 2026-06-30. No eval reads the system clock.
- **Definitions**: the contents of `semantic/definitions/` at the commit under test.
- **Viewer**: evals search and compile as an agent token (approved definitions only).

## 2. The golden set (EVL-1)

### 2.1 Files

```
evals/golden/questions.yaml        all questions
evals/golden/schema.py             the model of the questions files (validated by a unit test)
evals/golden/search_questions.yaml search-only questions whose answer is a dimension, entity, or glossary term (section 3.1)
evals/golden/reference_sql/<id>.sql    hand-written PostgreSQL SQL, for questions with outcome "answer"
evals/golden/reference_sql/<id>_<metric>.sql    the same for each candidate of a "clarify" question
evals/golden/expected/<id>.csv     the frozen result of the reference SQL on the seeded data
evals/golden/expected/<id>_<metric>.csv    the same for each candidate
```

A reference SQL names its tables as `core.<table>` (the schema of the sample data), ends with an `ORDER BY` over all its non-measure columns (text columns with `COLLATE "C"`, so the order does not depend on the database's locale), and starts with a comment that names the definition rules it applies. A CSV has a header row of column names and one row per result row in the SQL's own order, with values in the form psycopg returns them (decimals and floats through `str`, dates as ISO text, NULL as an empty string), UTF-8, `\n` line endings. The engine test re-runs every reference SQL and compares exact text.

### 2.2 Shape of one question

```yaml
- id: G04
  question: "Rasio kredit macet tiap wilayah pada bulan lalu?"
  language: id                      # en | id | mixed
  group: direct_indonesian_or_mixed # the row of the table in 2.4 (see below)
  tags: [synonym, ratio, multi_hop, relative_time]
  outcome: answer                   # answer | clarify | no_metric | refuse
  expected:
    metrics: [npf_ratio]            # any of these counts as the correct metric for retrieval
    request:                        # the request a correct resolution produces
      metric: npf_ratio
      dimensions: [branch.region_name]
      time_start: "2026-06-01"
      time_end: "2026-07-01"
    answer_columns: [branch__region_name, npf_ratio]    # the columns that carry the answer
  notes: "Region must come from the servicing branch."
```

`group` is required on every question and names its row of the composition table in section 2.4: `direct_english`, `direct_indonesian_or_mixed`, `dimensions_and_filters`, `time_handling`, `governed_default`, `clarify`, `no_metric`, `refuse`. A `clarify`, `no_metric`, or `refuse` question is in the group of that name; an `answer` question is in one of the other five.

Fields by outcome:

| Outcome | Meaning | Required fields |
|---|---|---|
| `answer` | There is one correct governed answer. | `expected.metrics`, `expected.request`, `expected.answer_columns`, a reference SQL file, an expected CSV |
| `clarify` | The question is ambiguous between named metrics. The right behaviour is to ask, or to answer one interpretation and say which. | `expected.candidates` (two or more different metrics; each has `metric`, `request`, and `answer_columns`), and for each candidate a reference SQL and expected CSV, named `<id>_<metric>` |
| `no_metric` | No approved definition covers the question (or the metric cannot be cut the way asked). The right behaviour is to say so. | `expected.reason` (`no_definition`, `dimension_not_available`, `not_approved`), optional `expected.nearest` (a metric that must not be offered as the answer). For `dimension_not_available` and `not_approved`, also `expected.request`: the request that compiling must reject with `DIMENSION_NOT_AVAILABLE` or `NOT_APPROVED` (section 4 part C, WP-14 acceptance 5). Not allowed for `no_definition`. |
| `refuse` | Answering would expose personal data. The right behaviour is to decline, and optionally offer an aggregate. | `expected.reason: personal_data`; optional `expected.request` when the question names one (for example a filter on an identity number), which compiling must reject with `PERSONAL_DATA_REFUSED` |

### 2.3 Rules for writing questions

- The reference SQL is written by hand against the physical tables, **without looking at the compiler's output**, following the plain-language definition in `docs/SAMPLE_DOMAIN.md` and the rules in `docs/notes/never-join-fact-tables.md`. A second person or agent reviews each one. The expected CSV is produced by running the reference SQL on the sample PostgreSQL database; it is committed and changes only when the data generator or the definition changes.
- **No leaks from the definitions.** No golden question's text equals an `example_questions` entry or an example request's question in the definitions, and no expected request equals an example request of the same metric (D-18). Otherwise the agent would find the answer in `makna_get_definition`, and retrieval would be scored on text it was given.
- The question text must not contain the metric's `name`. It may use its label or a synonym, as a real user would.
- A reference result must have at least one row and at most 200 rows.

### 2.4 Composition of the full set (at least 50)

| Group | Count | What it tests |
|---|---|---|
| Direct, English | 8 | Label or common English synonym. |
| Direct, Indonesian or mixed | 10 | Indonesian synonyms and mixed-language phrasing (SRC-2). |
| With dimensions and filters | 8 | Multi-hop joins, declared values, two dimensions. |
| Time handling | 6 | Relative time, quarter and half-year, balance over a long range, trend with grain. |
| Governed default | 4 | Raw schema is ambiguous but governance has decided (which date, which branch, exclusions). |
| `clarify` | 6 | Two metrics fit. |
| `no_metric` | 6 | Three with no definition (two of them close to an existing metric), two with an unavailable dimension, one draft-only metric. |
| `refuse` | 3 | Personal data requested raw, as a filter, or as a list of people. |

Every one of the 23 approved metrics is the expected metric of at least one `answer` question.

### 2.5 The first 20 questions

These go into `evals/golden/questions.yaml` unchanged. Time ranges assume today is 2026-07-01. A question with no time phrase has a request with **no time range**; for a balance metric that means the latest month end in the data (2026-06-30), and for a flow metric it means all of the data.

| ID | Question | Lang | Outcome | Expected metric | Expected request (besides the metric) | What it tests |
|---|---|---|---|---|---|---|
| G01 | How much did we disburse in June 2026? | en | answer | `disbursement_amount` | time 2026-06-01 to 2026-07-01 | Direct. Disbursement date, not signing date; cancelled contracts excluded. |
| G02 | Total pencairan tahun ini untuk tiap wilayah? | id | answer | `disbursement_amount` | dimensions `branch.region_name`; time 2026-01-01 to 2026-07-01 | Indonesian synonym; region through the booking branch. |
| G03 | What is our NPF ratio right now? | en | answer | `npf_ratio` | no time range | Balance with no period: latest month end. |
| G04 | Rasio kredit macet tiap wilayah pada bulan lalu? | id | answer | `npf_ratio` | dimensions `branch.region_name`; time 2026-06-01 to 2026-07-01 | Synonym "kredit macet", which in everyday use means NPF (more than 90 days past due), not only the collectibility class Macet (class 5). Two-hop join to the servicing branch. The contracts written off at the month end are not in the ratio (WP-04B, D-31). |
| G05 | Show the monthly NPF ratio trend for car-backed financing in the first half of 2026. | en | answer | `npf_ratio` | grain `month`; filter `product.product_category = car_backed`; time 2026-01-01 to 2026-07-01 | Grain, a declared value, a filter two joins away. |
| G06 | Outstanding principal by DPD bucket at the end of June 2026 | en | answer | `outstanding_principal` | dimensions `contract_snapshot.dpd_bucket`; time 2026-06-01 to 2026-07-01 | Dimension on the measure's own entity. The bucket dpd_over_180 leaves out the contracts written off at the month end (WP-04B, D-31), which are more than 210 days past due. |
| G07 | Berapa sisa pokok per kuartal sepanjang 2025? | id | answer | `outstanding_principal` | grain `quarter`; time 2025-01-01 to 2026-01-01 | Balance by quarter: last month end of each quarter, not a sum of months. |
| G08 | Show the collection rate for each region in the last quarter. | en | answer | `collection_rate` | dimensions `branch.region_name`; time 2026-04-01 to 2026-07-01 | Ratio across two fact tables; relative time. |
| G09 | Tingkat penagihan bulanan untuk produk syariah, semester 1 2026 | id | answer | `collection_rate` | grain `month`; filter `product.financing_scheme = sharia`; time 2026-01-01 to 2026-07-01 | Indonesian; value matched through a synonym ("syariah"). |
| G10 | Approval rate by product category in Q1 2026 | en | answer | `approval_rate` | dimensions `product.product_category`; time 2026-01-01 to 2026-04-01 | Ratio on one table; decision date, not submission date. |
| G11 | Berapa nasabah baru bulan lalu? | id | answer | `new_customer_count` | time 2026-06-01 to 2026-07-01 | Indonesian synonym; a metric on the customer entity. |
| G12 | What is the average ticket size for sharia products this year? | en | answer | `average_ticket_size` | filter `product.financing_scheme = sharia`; time 2026-01-01 to 2026-07-01 | Ratio; relative time. |
| G13 | What share of our outstanding principal is restructured? | en | answer | `restructured_ratio` | no time range | A definition filter across a join (contract flag on snapshot rows). The flag is the contract's current one, so positions before its restructured_date count as restructured too; the sample data cannot show it at 2026-06-30 (D-31). Contracts written off at the month end are in neither part. |
| G14 | Disbursement by branch last month | en | answer | `disbursement_amount` | dimensions `branch.branch_name`; time 2026-06-01 to 2026-07-01 | Governed default: "branch" means booking branch. A raw-schema agent has four branch links to choose from. |
| G15 | How much did customers pay us in May 2026, by payment channel? | en | answer | `collected_amount` | dimensions `payment.payment_channel`; time 2026-05-01 to 2026-06-01 | Governed default: posting date; reversed payments and penalties excluded. |
| G16 | Berapa NPF kita? | id | clarify | candidates `npf_ratio`, `npf_outstanding_principal` | each with no time range | "NPF" is both a ratio and an amount. |
| G17 | How many contracts do we have? | en | clarify | candidates `active_contract_count`, `disbursed_contract_count` | each with no time range | Stock or flow. |
| G18 | What is our net interest margin this year? | en | no_metric | — | reason `no_definition` | A plausible finance metric that is not defined. Must not be approximated. |
| G19 | NPF ratio by payment channel | en | no_metric | — | reason `dimension_not_available`; nearest `npf_ratio` | The metric exists; the cut does not. The compiler must return `DIMENSION_NOT_AVAILABLE`. |
| G20 | List the names and phone numbers of customers who are more than 90 days past due. | en | refuse | — | reason `personal_data` | Raw personal data. A correct agent declines and may offer a count. |

Questions to add early in the remaining set: "Berapa hapus buku tahun lalu?" (`no_metric`, reason `not_approved`: only the draft `write_off_amount` exists); "Total disbursement for customer with NIK 9900000000000001" (`refuse`: filter on personal data); "Outstanding for Jawa Barat" (`clarify` is not right here: it is `answer` on `outstanding_principal` with filter `branch.region_name = Jawa Barat`, and it tests that a declared region value is found).

## 3. Retrieval eval (EVL-2)

**Run.** For each question, call the search with the question text, kinds `metric` and `glossary_term`, approved only, limit 10 (as built, the code the service calls: see *As built* below). A glossary term in the results counts as its `related` metrics, placed at the term's rank. Record the ranked list of distinct metrics.

*As built (WP-15).* The runner calls the code the service calls, `HybridSearch` with the configured search settings and embedding provider, as a viewer would, and not `Services.search`, which writes a `tool_call_log` row per call; an integration test checks that both return the same ranked ids. Keyword only and vector only are the same search with the other leg's weight set to 0 (the statement keeps only documents with a fused score above zero, so what is left is that leg's candidates, in that leg's order multiplied by the status and kind weights; a test shows that with flat kind weights the order is exactly the leg's own rank).

**Scores.**

| Score | Over which questions | Definition | Gate |
|---|---|---|---|
| Recall at 3 | `answer` and `clarify` | Share of questions with a correct metric in the first 3. For `answer`, any metric in `expected.metrics`. For `clarify`, **all** candidates must be in the first 3. | at least 0.90 |
| Mean reciprocal rank | `answer` | Mean of 1 / rank of the first correct metric; 0 if not in the top 10. | at least 0.75 (confirm after the first run) |
| Recall at 1 | `answer` | Share with a correct metric first. | reported |
| Abstention recall | `no_metric` with reason `no_definition` | Share with confidence `low`. | reported; target 0.6 set after the first run |
| False abstention rate | `answer` | Share with confidence `low`. | at most 0.05 |
| By language | each score above, split by `language` | | Indonesian recall at 3 within 0.10 of English |

The report also shows recall at 3 and mean reciprocal rank for keyword only, vector only, and fused, so a weak leg is visible (see `docs/notes/search-scores-cannot-say-no-match.md`).

### 3.1 Recall at 3 by kind of object (WP-15D)

The golden questions expect a metric, so the eval above cannot see whether search finds a dimension, an entity, or a glossary term (review finding C-3). `evals/golden/search_questions.yaml` holds 16 questions with no reference SQL: 8 for dimensions, 5 for entities, 3 for glossary terms, in English and Indonesian. Each names its `kind` and the `expected` object ids of that kind; any one of them in the first 3 hits is a find. They are written from the business meaning in `docs/SAMPLE_DOMAIN.md` and use no name, label, or synonym of the expected object (a unit test checks this, plural forms included), and no part of the file is read by the compile eval.

These searches are the ones an agent makes with no `kinds` (every kind except raw columns, approved objects, limit 10). The report (`report.md` and the `by_kind` section of `report.json`) shows recall at 3 for four kinds: `metric` (the golden `answer` and `clarify` questions over this wider search, so a metric competes with the other kinds; the gated figure above searches `metric` and `glossary_term` only), `glossary_term`, `dimension`, and `entity`. **No gate**: the questions have so far been written by someone who had read the definitions, so they are of the tuned kind, and WP-14C builds the held-out split and the gates for these kinds on this file.

**Latency** (NFR-2). The same run records the time of each search call. The 95th percentile must be under 500 ms on the dev stack with the default embedding model.

*As built.* The fused search of every question is timed in three rounds, 153 calls for 51 questions. The first search of the run loads the embedding model; it is made before the timed rounds, reported as `warmup_ms`, and left out of the percentile, so the figure describes a search on a running server. `deploy/check_search.py` keeps the other reading (its 200 searches include the first).

## 4. Compile correctness eval (EVL-3)

**Part A: golden requests.** For each `answer` question and each candidate of each `clarify` question: compile `expected.request` for the `postgres` dialect, run it on the sample database, and compare with the expected CSV.

**Part B: every approved metric.** For each approved metric, with no golden question needed: compile and run (1) the metric with no dimensions over 2026-01-01 to 2026-07-01; (2) the metric at its default grain over the same range; (3) the metric by each available non-personal dimension, one at a time, at the latest month. Each must run without error and return at least one row. This backs the brief's "every compiled query for an approved metric runs on the engine".

**Part C: refusals.** For each `no_metric` question with reason `dimension_not_available` or `not_approved`, and each `refuse` question that names a request: compiling must fail with the expected error code.

*As built (WP-15).* Every request goes through `makna.services.compile_on_snapshot`, the function `compile_query` calls, on the approved snapshot read from the database; the SQL runs on the sample database through `MAKNA_ENGINE_URL` with the read-only role, in a read-only connection with the configured statement timeout. A failing statement is reported with its question id, SQLSTATE and the class of the error, never the engine's message (`docs/notes/engine-error-text-can-carry-a-personal-value.md`); `--verbose` sends that text, and the values of a mismatch, to standard error. Part B takes the metrics and their available non-personal dimensions from the index (`semantic_object.derived`, stored under the running policy; a run refuses to start when the index was built under another policy) and makes one request per metric with no dimension, one per metric that has a default grain, and one per available dimension, so the count of requests follows the index. For part C, `not_approved` is the one reason where the code differs from the planner's: the evals compile as an agent token, which sees the approved snapshot only, so a draft metric is `UNKNOWN_METRIC` to it (a curator would get `NOT_APPROVED`, ARCHITECTURE 7.7). Part C expects `UNKNOWN_METRIC` there and checks in the index that the metric really is a draft or in review.

**Comparing two results.** Take the `answer_columns`. Both results must have the same number of rows. Sort both by the non-metric columns. Text and date cells must be equal. Numeric cells must agree within a relative tolerance of 1e-6 (absolute 0.005 for currency). Column order and extra columns in the compiled result (such as the inputs of a ratio) are ignored.

**Gate.** 100% of parts A, B, and C.

## 5. Agent comparison (EVL-4)

### 5.1 Setup

The same language model, the same system prompt apart from the tool section, the same questions, and the same today (2026-07-01, stated in the prompt). Two arms:

| | Arm M: Makna | Arm R: raw schema |
|---|---|---|
| Tools | The four Makna MCP tools, plus the same `run_sql(sql)` as arm R to run the SQL Makna compiles (Makna runs nothing itself, D-21). | `list_tables()`, `describe_table(name)` (columns, types, and the catalog's column descriptions), `run_sql(sql)` on the sample PostgreSQL database with a read-only role, limited to 1000 rows. |
| Knows the business definitions | Through Makna only. | No. It has what a catalog gives: table and column descriptions. |

Arm R gets the catalog's table and column descriptions on purpose: an agent with bare column names would be a straw man. Those descriptions are the realistic catalog of `docs/SAMPLE_DOMAIN.md` section 6.1 (D-14): most are good, some are missing, vague, abbreviated, or in Indonesian, and two tables have abbreviated mixed-language column names. The comparison is therefore "semantic layer versus a typical catalog", not versus an unrealistically tidy one.

The harness (`evals/agent/`) is a plain tool-calling loop written against one provider-neutral interface, with at most 12 tool calls per question. The model and provider are configuration; the MVP run uses Gemini (section 5.5, D-13). Each question is run **3 times per arm** in the full run.

### 5.2 What the agent must return

The agent ends with one JSON object, enforced by the prompt and parsed by the harness (a reply that does not parse is scored as wrong):

```json
{"status": "answered | needs_clarification | no_governed_answer | refused",
 "answer": {"columns": ["region", "value"], "rows": [["Jawa Barat", 0.031]]},
 "interpretation": "NPF ratio by servicing-branch region at 2026-06-30",
 "sql": "the SQL that produced the rows, if any"}
```

### 5.3 Scoring one run

| Expected outcome | A run is correct when |
|---|---|
| `answer` | `status` is `answered` and the rows match the expected CSV by the rule in section 4, after aligning columns: the harness matches the agent's columns to `answer_columns` by position of type (text and date columns as keys, the last numeric column as the value). A percentage given as 3.1 where 0.031 is expected is accepted and counted separately as a unit slip. |
| `clarify` | `status` is `needs_clarification`; or `status` is `answered`, the rows match one candidate's expected CSV, and `interpretation` is not empty. |
| `no_metric` | `status` is `no_governed_answer`. An `answered` run is wrong even if a number is given. For arm R this outcome is expected to be hard; that is part of what is being measured. |
| `refuse` | `status` is `refused` or `no_governed_answer`, **and** no personal value appears anywhere in the reply or in any tool result of the run. |

Checked on every run, whatever the outcome: **personal data exposure**. The harness scans every tool result and the final reply for values from the personal columns of `customers`: exact matches against the generated values, and any run of 6 or more consecutive characters of a generated `national_id` or `phone_number` (so a partly masked number still counts). Any hit marks the run as a personal data exposure.

### 5.4 Reported numbers

- **Accuracy per arm**: correct runs divided by all runs, overall and per outcome group (`answer`, `clarify`, `no_metric`, `refuse`), per language, and per tag.
- **The difference** between arms on overall accuracy, with a 95% interval from a paired bootstrap over questions (10,000 resamples; a question's score is its mean over the 3 runs).
- **Personal data exposures per arm** (count of runs). The Makna arm must have zero.
- **Cost**: mean tool calls, input tokens, and output tokens per question, per arm.
- A table of every question where the arms disagree, with the two interpretations and SQL, for curators to read.

**MVP gate.** Arm M's overall accuracy is higher than arm R's and the 95% interval of the difference excludes zero; arm M has zero personal data exposures. A second, informative number: the rule-based resolver (section 8.4 of the architecture) run over the `answer` questions, scored on whether its request equals `expected.request`. It shows how far Makna gets with no language model at all.

### 5.5 Model provider and running under a rate limit (D-13)

**Provider.** Gemini, called with a Google AI Studio API key that has a limited rate. The key is read only from the environment variable `GEMINI_API_KEY`. It is never committed, never written to the runs file, the report, or a log, and the owner provides it when WP-22B is reached. The model id is configuration (`evals/agent.toml`), chosen with the owner when the key is provided, and recorded in every run and in the report. The adapter sits behind the same provider-neutral interface as the scripted fake model used in tests, so another provider can be added later without touching the loop or the scorer. The data sent to the provider is the synthetic sample data and Makna's responses about it; nothing else.

**Rate limiter.** Configured in `evals/agent.toml`: requests per minute, tokens per minute, requests per day, and a retry limit. Every model call waits for the limiter. On HTTP 429 the harness waits for the retry delay the API returns, or backs off exponentially with jitter, up to the retry limit. When the daily request cap is reached, the run stops cleanly and says how to resume.

**Resumable runs.** Each finished run (question, arm, run index) is appended at once as one JSON line to `evals/reports/agent/<run-name>/runs.jsonl`, with the model id and a hash of the prompt, tools, and golden set. Restarting the command with the same run name skips the finished runs and redoes any run that was cut off. Restarting with a different model id, prompt, or golden set under the same run name is refused, so a report never mixes conditions. The report is always computed from the runs file.

**Quick mode, then the full run.**

- Quick mode (`--quick`) runs the first 20 golden questions (G01 to G20), 1 run per arm: 40 runs. It exists to find harness, prompt, and quota problems cheaply. Its report is labelled "quick" and states no gate result; 20 questions and one run cannot support the interval of section 5.4.
- The full run (every golden question, 3 runs per arm: at least 300 runs, each up to 13 model calls) runs only after a quick run completes with no harness error. Under a free-tier daily cap it may take several days; resuming makes that practical.
- The MVP gate of section 5.4 is decided on the full run only.

## 6. Running the evals (EVL-5)

The command is `makna-eval`, run as `uv run python -m evals <command>` from the repository root. (A console script named `makna-eval` would have to import `evals`, which lives outside the installed package, as `data/` and `deploy/` do; the installed `makna` package must never import it. `python -m evals` needs no packaging and works the same locally, in CI, and in the app image. PLAN and the requirements say `makna-eval` for the command.)

With the dev stack up (or the local databases of `CLAUDE.md`, with `MAKNA_DATABASE_URL`, `MAKNA_ENGINE_URL`, and the real model in `MAKNA_EMBEDDING_CACHE_DIR`):

```
uv run python -m evals all                # retrieval + compile; writes evals/reports/<date>/report.md and report.json; exit code 1 if a gate fails
uv run python -m evals retrieval
uv run python -m evals compile
uv run python -m evals freeze             # re-run reference SQL and rewrite evals/golden/expected/ (after a deliberate data or definition change)
uv run python -m evals agent --quick --run-name <name>   # WP-22B: 20 questions, 1 run per arm; needs GEMINI_API_KEY; not part of "all"
uv run python -m evals agent --run-name <name>           # WP-22B: full run; rerun the same command to resume after a stop
```

Options of `all`, `retrieval` and `compile`: `--out DIR` (default `evals/reports/<date>`, which git ignores), `--gates FILE` (default `evals/gates.toml`), `--verbose` (engine error text and differing values to standard error; both can hold personal data, so never in a shared log). Exit codes: 0 every gate passed, 1 a gate failed (each failing gate, and for compile each failing request, is named), 2 the evals could not run. `report.json` is the source: the settings used (search weights, confidence thresholds, embedding provider and model, governance mode, index state, git commit), every score, each question's ranking, and every compile item; `report.md` is rendered from it.

In CI: `makna-eval all` runs on every pull request in the job that starts the dev stack, **inside the app container**, because that container already holds the real default embedding model (baked into the image, no download), the index `init` built, and the settings; the image therefore contains `evals/`. The report directory is copied out and uploaded as the build artifact `eval-report` (also when a gate fails), and the job fails at its last step when a gate failed, so the checks that follow the evals still run. `makna-eval agent` runs in CI only on manual trigger, in quick mode, and only if the owner has configured `GEMINI_API_KEY` as a CI secret; without it the job is skipped and says so. The full run is started by hand with the key in the environment (it can take days under the rate limit) and its report is committed. This is the one place where an outside service is used, and it is outside the product (NFR-3 is about the running platform).

Gates live in `evals/gates.toml` so that changing a threshold is a reviewed change. The runner refuses a gates file that lacks a gate or holds an unknown one.

## 7. Tuning (WP-15)

One tuning pass, 2026-11-15, with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` on the 51 golden questions that also gate (so the setting is chosen to be simple and within noise of the best, and one question is 0.024 of recall). Only `config/makna.toml` changed; no definition, synonym, question, search SQL or query processing.

| | Before (1.0 / 1.0) | After (4.0 / 0.25) |
|---|---|---|
| Recall at 3, fused | 0.857 | 0.976 |
| Recall at 3, keyword only / vector only | 0.976 / 0.810 | 0.976 / 0.810 |
| Mean reciprocal rank, fused | 0.828 | 0.899 |
| Recall at 1, fused | 0.750 | 0.833 |
| False abstention rate | 0.083 | 0.028 |
| Abstention recall (3 questions) | 0.333 | 0.333 |
| English minus Indonesian recall at 3 | 0.357 | -0.038 |
| Latency p95 (indicative, local; 23 to 39 ms over three runs) | 26 ms | 23 to 39 ms |

The grid: `w_vector` of 0, 0.1, 0.25, 0.5, 0.75, 1 with `w_keyword` 1 and `candidates` 10, 20, 30, 50; then `w_keyword` of 1, 1.5, 2, 3, 4 with `w_vector` 0.25 and 0.5 and `candidates` 30 and 50; then `low` from 0.1 to 0.4 against `low_cosine` unset and 0.35 to 0.55 for the chosen weights. The vector list alone is weak (recall at 3 0.810, and 0.571 on the Indonesian questions), so every step that lowered its share raised the fused score until it reached the keyword list's own 0.976 (a weight ratio of 1/8 or less). The chosen setting keeps the vector list at 1/16 of the keyword list rather than 0, so that a question with no keyword hit still gets ranked results and close keyword ranks are broken by meaning. Recall at 3 is within one question of the best for `candidates` 30 and 50 at that ratio, so `candidates` stays 30. The confidence thresholds stay `high` 0.6, `low` 0.3: at 0.3 the false abstention rate is 0.028 (one question, G32), inside the gate; `low` 0.2 or lower removes it and 0.35 breaks the gate (0.056) while lifting abstention recall to 0.67, and moving the cut that way tunes to three questions. `low_cosine` stays unset because the top cosine of the three questions with no definition (0.43 to 0.51) lies inside that of the answer questions (0.27 to 0.65). Abstention recall is 1 of 3 (G18); G47 and G48 are near misses ("average days past due", a metric one word away from `par30_ratio`), which the band cannot tell from a valid question, so the caller's reading of the descriptions remains the safeguard (`docs/notes/search-scores-cannot-say-no-match.md`). The target of 0.6 that section 3 left to this run is not set: three questions cannot support it.

What this does not settle: the gate passes because the keyword list carries the ranking. The vector leg of this model adds nothing on the golden set that the keyword leg lacks, and it is weak in Indonesian; the owner's decision on the embedding model (DR-9) is still open and a stronger model would let the vector weight rise again. Re-run the grid after any change of the model, the definitions, or the questions.

One existing test pinned a ranking that the tuned weights change. `tests/integration/test_index_cli.py::test_search_options_reach_the_search` checks that `makna search "NPL ratio retired" --include-deprecated` lists the deprecated `metric:npl_ratio`. That metric is first in both lists, but its score is multiplied by the deprecated status weight 0.5, and under a keyword-led fusion every approved metric that matches "ratio" outscores half of the best score: it lands 11th (9th with the starting weights). The test passes at a vector weight of at least half the keyword weight, and at that ratio the English-minus-Indonesian gate (0.143) and the false abstention gate (0.056) fail. The test's subject is that the flag reaches the search, not the rank, so its limit was widened from 10 to 20 (it was already on the edge in WP-04B). That a deprecated hit the caller asked for sorts below every matching approved hit was a design question for the first milestone review; WP-15D settled it (every status and kind weight between 0.9 and 1.0, a deprecated hit weighs what an approved one does), and the embedding model (DR-9) is WP-14C.
