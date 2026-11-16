# Milestone Review 1: Core Complete

Date: 2026-11-16
Reviewed commit: `a2608444644f087609265da76dcb54079b71bdfa` (`main`, milestone 1 complete: 18 of 30 work packages)
Reviewer: independent milestone review (fresh context). Material read: `CLAUDE.md`, `docs/PROJECT_BRIEF.md`, `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/EVALS.md`, `docs/PLAN.md`, `docs/progress.json`, `docs/notes/`, then the code and tests on `main`.

## Verdict

**Ready to move on after named fixes**: the agent path works as designed and I found no way for an agent to read the raw value of a personal column, approve a definition, or make Makna write a join that is not declared, but two things must be fixed before milestone 2 builds on them (C-1 and C-2 below).

The two fixes:

1. **C-1.** `makna index build` and the stack's `init` serve definition files that fail `makna validate` as approved. Make both refuse.
2. **C-2.** The retrieval gate only shows that search finds a metric when the question repeats its synonyms. On 40 new questions that paraphrase instead, recall at 3 is 0.60 and more than half get the answer "no approved definition exists". Add a held-out split to the eval and soften the low-confidence guidance before any more tuning and before the agent eval (WP-22, WP-22B) is built on this search.

Counts: 2 High, 5 Medium, 6 Low. Six findings are proven by tests on this branch (`tests/unit/test_review_core_complete.py`, `tests/integration/test_review_core_complete.py`). The tests are marked as expected failures so that the suite stays green; remove a marker to see the failure.

Everything below was observed in this session. Where I could not confirm something, the text says "unconfirmed".

## 1. What I ran, and the numbers

Environment: a local PostgreSQL 16 with pgvector, the generated sample data, the real default embedding model from a local cache, Python 3.13 (CI uses 3.12). No Docker, so I did not start the development stack; CI on GitHub Actions is the proof for it.

| Check | Command | Result |
|---|---|---|
| Unit tier | `uv run pytest -m unit` | 2,304 passed in 101 s |
| Integration tier | `uv run pytest -m integration` | 337 passed in 139 s |
| Engine tier | `uv run pytest -m engine` | 95 passed in 51 s |
| All tiers in one run | `uv run pytest` | 2,736 passed in 264 s, none skipped |
| Lint, format, types | `ruff check .`, `ruff format --check .`, `mypy` | all clean (229 files) |
| Evals | `uv run python -m evals all` | every gate passed (table below) |
| CI on `main` at the reviewed commit | run 43776089048 | three jobs green: lint, types and unit; integration; development stack (with the engine tests, the evals, and the offline stack) |

Eval gates, my run (CI printed the same scores; only the latency differs, 20.7 ms there):

| Gate | Value | Required |
|---|---|---|
| Recall at 3, fused | 0.976 | at least 0.90 |
| Mean reciprocal rank, fused | 0.899 | at least 0.75 |
| False abstention rate | 0.028 | at most 0.05 |
| English minus Indonesian recall at 3 | -0.038 | at most 0.10 |
| Search latency, 95th percentile | 12.2 ms | under 500 ms |
| Compile part A (golden requests) | 48 of 48 | 100% |
| Compile part B (every approved metric) | 719 of 719 | 100% |
| Compile part C (refusals) | 4 of 4 | 100% |

Other scores from the same report: recall at 1 is 0.833. Keyword only: recall at 3 0.976, recall at 1 0.833, mean reciprocal rank 0.899, which is identical to the fused search on all three. Vector only: 0.810, 0.556, 0.667; on the Indonesian questions 0.571, 0.250, 0.415. Abstention recall is 1 of 3.

One caveat on my compile numbers: the local sample database is not named `lending`, so the eval runner removes the `lending` qualifier and prints the SQL again before it runs it (`evals/sqlrun.py`). Only CI runs the compiler's text untouched. CI passed.

I also started `makna serve` and called `/mcp` over HTTP with `curl`: no token and a wrong token give 401, `GET` gives 405, another `Host` gives 421, `tools/list` returns the four tools, all marked read-only, a call to a tool named `makna_run_query` is refused, and a request for `customer.full_name` is refused with `PERSONAL_DATA_REFUSED`.

## 2. Findings, in order of severity

### High

**C-1. `makna index build` serves a definition that fails validation as approved.**

- What is wrong: the build refuses only files it cannot read (V01). It does not run the cross-file rules V02 to V20, so a file that `makna validate` rejects becomes an approved object that agents find, read, and compile. The stack's `init` step does the same. Until WP-18 exists, this build is the only gate between a directory and "approved".
- Where: `src/makna/cli/main.py` (`_index_build`), `deploy/init.py` (`build_index_step`).
- Evidence: I added one metric file whose filter value is misspelt (`contract_status = actve`, V10) and whose example request cannot compile (V19). `makna validate` exited 1 with both errors. `makna index build` exited 0. An agent token then found the metric first in search, `get_definition` returned it as approved with the broken example request, and `compile_query` returned SQL with `"contract_status" = 'actve'`, which returns NULL on the sample data: a wrong number with no error. Test: `test_index_build_does_not_serve_a_definition_that_fails_validation`.
- What limits the damage: the compile-time checks still hold for personal data (the final check, the function allow-list, the reference issues), so this is a wrong-answer problem, not a leak.
- Smallest fix: in both places, run `validate(..., extra_checks=[check_example_requests])` before indexing and refuse to index when it reports an error, as the build already does for unreadable files. WP-18 must keep that for `index build` from `HEAD`.

**C-2. The retrieval result does not carry over to questions that do not repeat the synonyms, and the confidence band then tells the agent that no definition exists.**

- What is wrong: the 51 golden questions both tuned the weights and gate them, and they were written with the definitions in view. 25 of the 36 `answer` questions contain a label or synonym of their expected metric word for word, and most of the rest share its key word. The gate therefore measures a synonym lookup. The tuning pass then moved the weights to where that lookup alone decides (keyword 4.0, vector 0.25).
- Where: `evals/golden/questions.yaml`, `config/makna.toml` `[search]`, `src/makna/search/confidence.py`, `docs/EVALS.md` section 7.
- Evidence: I wrote 40 new questions (20 English, 20 Indonesian, one or two per metric) that say the same thing in other words, for example "How much money did we lend out last month?" for `disbursement_amount`. They are in `docs/reviews/core-complete-probes/heldout_retrieval.py`. Searched as the eval searches:

  | Search | Recall at 3 | Recall at 1 | English | Indonesian | Confidence `low` |
  |---|---|---|---|---|---|
  | Tuned weights (4.0 / 0.25) | 0.600 | 0.350 | 10 of 20 | 14 of 20 | 22 of 40 |
  | Keyword only | 0.575 | 0.325 | 11 of 20 | 12 of 20 | 22 of 40 |
  | Vector only | 0.575 | 0.325 | 13 of 20 | 10 of 20 | 32 of 40 |
  | Starting weights (1.0 / 1.0) | 0.750 | 0.475 | 16 of 20 | 14 of 20 | 24 of 40 |

  Three things follow. First, on paraphrases the vector list is as good as the keyword list and the two find different questions, so equal weights beat the tuned weights by 15 points; the tuning removed exactly that. Second, 22 of 40 valid questions come back with confidence `low`, whose guidance is "Tell the user that no approved metric exists for this"; in 8 of those 22 the right metric was in the first three hits. Third, the band does not catch the opposite case either: of the three golden questions with no definition, G47 comes back `high` and G48 `medium`.
- Limits of this evidence: one reviewer wrote the 40 questions, after seeing the metric names and synonyms, and made them hard on purpose. The exact figure will move with another author. The direction will not.
- Smallest fix, in this order: (1) add a held-out split to the golden set, written by someone who has not read the synonyms (the owner, or a business user), report it next to the tuned split, and never tune on it; (2) change the `low` guidance from "no approved metric exists" to "no definition clearly matches; read the descriptions of the hits before deciding", because the band cannot support the stronger claim; (3) tune the weights again on both splits; (4) then decide the embedding model (DR-9). I could not test another model here: only the default model is in the local cache.

### Medium

**C-3. The status and kind weights work as hard tiers, not as preferences.**

- What is wrong: the fused score is a sum of `1 / (60 + rank)`. Rank 1 and rank 30 differ only by the factor 0.68. Any weight below that (deprecated 0.5, draft 0.5, column 0.6) puts the best match of its kind below every candidate of a full-weight kind, and 0.8 (dimension, entity, rule) puts it below about the 11 best metrics.
- Where: `SEARCH_SQL` in `src/makna/search/hybrid.py`, `[search.status_weights]` and `[search.kind_weights]` in `config/makna.toml`, `docs/ARCHITECTURE.md` section 8.2.
- Evidence (`docs/reviews/core-complete-probes/search_ranking.py`, real model): "region" is first in both lists for `dimension:branch.region_name`, and it lands 7th, behind six metrics that mention a region in an example question; the band is `low`. "payment channel" puts `dimension:payment.payment_channel` 9th. A curator who searches "write-off amount" finds the draft `write_off_amount` 8th. "NPL ratio" with `include_deprecated` does not show the deprecated `npl_ratio` in the first ten. The MCP default limit is 5, so an agent that searches for a dimension gets five metrics. Test: `test_a_search_for_a_dimension_returns_it_in_the_first_five`.
- Why the evals did not see it: the retrieval eval searches kinds `metric` and `glossary_term` only. SRC-1 names dimensions, entities and columns too, and nothing measures them.
- Smallest fix: keep every weight between about 0.9 and 1.0 so that it breaks ties instead of deciding, set `deprecated` to the same value as `approved` (a deprecated hit appears only when the caller asked for it, and the hit carries its status), and add dimension and entity questions to the retrieval eval.

**C-4. The minimum group size does not protect a person from an agent that tries, and the texts should not suggest it does.**

- What is wrong: this is inside the documented limits of DR-5, but the size of the gap is larger than "a documented limit" suggests.
- Evidence (`docs/reviews/core-complete-probes/privacy.py`, synthetic data): (a) `new_customer_count` by six customer dimensions that are not personal (city, occupation group, gender, income band, registration date, segment) returns 5,893 groups for 6,000 customers, and 5,797 of them are one customer. No personal dimension is selected, so no group is left out, and any metric can be read per customer this way. (b) Two requests by the masked `customer.birth_year`, the second with the filter `customer.registered_date != 2024-05-10` (a day on which one customer registered), differ in exactly one row: 1983, 147 against 146. That is the birth year of that customer, read through the mask.
- Where: `docs/ARCHITECTURE.md` section 7.6, the warning text in `src/makna/compiler/explain.py`.
- Smallest fix for this phase: say plainly, in `README.md` and in section 7.6, that the minimum group size prevents small cells in one result and is not a privacy guarantee, and that access control in the engine is the protection (agents run SQL with their own access, D-21). For phase 2, the candidates DR-5 already lists (refuse `!=` and `not_in` together with a personal dimension, a minimum group size for groups built from person-level dimensions in general).

**C-5. Definition expressions are read with sqlglot's default dialect, and two PostgreSQL spellings silently change meaning.**

- What is wrong: a curator on PostgreSQL writes PostgreSQL. `x ^ 2` (a power) is read as an exclusive or and printed as `x # 2`. `CAST(x AS int8)` and `x::int8` (eight-byte integer) are printed as `SMALLINT`. Both pass V05. The first gives a wrong number with no error; the second overflows above 32,767, and the overflow message of the engine quotes the value. `a / b` is printed with a floating-point numerator, which is deliberate (`docs/notes/integer-division-differs-between-postgres-and-duckdb.md`) but also not what PostgreSQL does with two integers.
- Where: `sqlglot.parse_one(sql, into=exp.Condition)` with no `read=` in `src/makna/definitions/validate.py` (`_parse`), `src/makna/compiler/sqlgen.py` (`_parsed`), `src/makna/model/sensitivity.py`, `src/makna/catalog/references.py`, `src/makna/compiler/governance.py`.
- Evidence: `docs/reviews/core-complete-probes/expression_dialect.py` prints 24 expressions both ways. Tests: `test_a_power_operator_is_refused_or_stays_a_power`, `test_an_int8_cast_is_refused_or_stays_a_big_integer`. The sample definitions use neither spelling, so no current number is wrong.
- Smallest fix: make V05 refuse what the two readings disagree on (`^`, `int8`, and any expression whose PostgreSQL print differs between the default parse and a PostgreSQL parse, division excepted), and write in section 5.1 that expressions are in sqlglot's neutral SQL, not PostgreSQL. Reading with the configured dialect instead is the cleaner end state, but it changes the division rule and needs a decision.

**C-6. Nothing checks that a relationship points at the key of its target, so "a join can never multiply rows" rests on an unchecked declaration.**

- What is wrong: section 7.3 says walking a relationship forward can never inflate a measure. That is true only if the target columns identify one row. V07 checks only that the target entity exists.
- Where: `src/makna/definitions/validate.py` (`entity_checks`), `src/makna/catalog/references.py`.
- Evidence: a fixture relationship from `order` to `customer` on `segment`, a column many customers share, passes validation with no issue. Test: `test_a_relationship_must_point_at_the_primary_key_of_its_target`. All sample relationships do point at primary keys, so no current number is wrong.
- Smallest fix: a validation rule that the `to` columns of a relationship are exactly the `primary_key` of the target entity, and a reference issue when the catalog says a declared primary key is not one (`is_primary_key`).

**C-7. The same logic exists in two or three places, and one of them decides what counts as a personal column.**

- What is wrong, where, and evidence:
  - "Which columns does this expression read" is written three times: `makna.model.sensitivity.columns_read`, `makna.catalog.references.columns_read`, `makna.definitions.validate._columns`. They already differ: on an expression that does not parse, the first answers "the dimension's own name" and the other two answer "no columns". The planner, the final check, V12, V18, V20, R02 and R03 each rely on one of them.
  - "The metric and everything it depends on" is written three times: `makna.compiler.plan._closure`, `makna.services.definitions._closure`, `makna.services.service._metric_closure`.
  - "The unit of a metric" is written three times: `plan._unit`, `services/definitions.py` line 158, `services/service.py` line 505.
- Why it matters now: milestone 2 adds the lifecycle and the editor, which validate, plan and index the same objects again. A fourth copy, or a fix applied to one copy, is the likely next bug, and for the first item it would be a personal-data bug.
- Smallest fix: one function for each in `makna.model` (the dependency rule allows every package to import it), and delete the copies.

### Low

**C-8. The default limit of 1,000 rows cuts results with no word about it.** `disbursement_amount` by `contract.disbursement_date` and `branch.branch_name` has 4,709 rows on the sample data; the compiled SQL returns 1,000, and neither `warnings` nor `explanation` mentions the limit (`src/makna/compiler/explain.py` has no sentence for it). An agent that adds up the rows reports a wrong total. Makna cannot know the row count (D-21), but it can say what it did. Smallest fix: when the request has a dimension or a grain, add one sentence: the result is cut at N rows in key order, and a result of exactly N rows may be incomplete.

**C-9. Some values pass the planner and fail on the engine.** A time filter value is accepted when Python's ISO parser accepts it, which is wider than PostgreSQL: `2026-W01-1` and `2026-01-01T10` compile and PostgreSQL rejects the cast (test: `test_a_time_filter_value_postgresql_cannot_read_is_an_invalid_filter`). A number against a text dimension with no declared values compiles (`customer.province = 5`) and PostgreSQL answers "operator does not exist: character varying = integer"; the planner cannot know the column type, but the service has the catalog's `data_type`. `limit: true` is accepted as 1. Section 8.4 promises an actionable error instead of SQL that fails; these are the exceptions I found. Where: `_type_fits` in `src/makna/compiler/plan.py`. Smallest fix: accept only `YYYY-MM-DD` and `YYYY-MM-DD HH:MM[:SS]` for time values, refuse a boolean limit, and consider the catalog type for values of dimensions that read one column.

**C-10. A deprecated entity that metrics reach by a join still compiles, with no warning.** I marked `product` deprecated in a copy of the definitions: `makna validate` exited 0 (V13 looks at the measure's entity, the entities its filters and pins name, and the input metrics, not at entities reached by an unpinned join), and `disbursement_amount` by `product.product_category` compiled with no warning. Section 5.2 says deprecated objects are refused by the compiler; for an entity that is only joined to, they are not. Smallest fix: decide what deprecating an entity means (I suggest: its dimensions stay usable, and the response carries a warning with the reason and replacement), then write that in section 5.2 and test it.

**C-11. A 1.4 MB golden file pins the exact SQL, warnings and explanation of 780 requests, and its instructions contradict each other.** `tests/unit/golden/compiler_governance_golden.json` proved WP-10B criterion 1, which was its job. Its script says "never regenerate it after a compiler change" and "regenerate it only after a change to the sample definitions, with the compiler of `main`". Every intended change to compiler output (the fixes for C-5 and C-8, any new warning) and every edit of a sample definition will fail 780 cases with no agreed way forward. Smallest fix: now that WP-10B is merged, either retire the file in favour of the behaviour tests that exist, or write down one regeneration procedure and who approves its diff.

**C-12. The tool text promises things the services do not do.** The `makna_search` description says "If `ambiguous_between` is present, ask the user to choose"; no service returns it before WP-20 (section 12 records this). The effect is visible in the eval: the six `clarify` questions depend entirely on the agent noticing two close hits. Not wrong today, but WP-22B will measure it; build `ambiguous_between` before that eval or take the sentence out until it exists.

**C-13. `docs/progress.json` marks requirement IDs under packages that are done, although several are only partly met at this milestone.** This is how the plan splits the work, not an error, but a reader of the file can take it for more: NFR-1 (the stack has no web app yet), WEB-5 (users and tokens exist; sign-in is WP-16), SEM-3 (files are validated against the published schema; the version-controlled write path is WP-18), SEM-4 and GOV-2 (the data exists; the report pages are WP-19C), NFR-5 (the agent eval is not in CI yet), SRC-1 (see C-3). Smallest fix: a `partly` note per requirement, or a sentence at the top of the file.

## 3. Security and governance: the four questions

**Can an agent reach raw personal data?** Not the raw value of a column marked personal, by any path I tried. Through the compiler: a personal dimension without a mask is refused, a filter on one is refused, the masked birth year returns first-of-January dates, and the final check on the syntax tree backs the planner. The property test (`tests/unit/test_compiler_property.py`) runs 600 seeded requests on DuckDB and compares every result cell with every raw personal value; it is not a vacuous test, it asserts how many requests compiled and how many groups it checked. Through search and definitions: a token cannot see the catalog column `customers.national_id` (`NOT_FOUND`, and it is absent from a `kinds: [column]` search that a curator's search returns first), and the dimension document shows the description and no values. Through the log: an email, a 16-digit number, a phone number and an identity number in a query were all stored as `[REDACTED]`, and a refused personal filter stored the dimension name only. What an agent can reach is per-person rows through dimensions that are not marked personal, and a masked value by comparing two results (C-4). And an agent that has its own engine access can run any SQL it likes: Makna does not sit between the agent and the engine (D-21).

**Can an agent approve a definition?** No. The application has two routes, `/healthz` and `/mcp`; the MCP server has four tools, all read-only, and refuses any other tool name. A token acts as a viewer whatever its user's role (an admin's token is `effective_role` viewer; I used one for every probe).

**Can a non-approver approve?** Today, approval is not enforced by anything, because the write path is WP-18. Whoever can write a file with `status: approved` into the definitions directory and run `makna index build` makes it official, and C-1 means the file need not even be valid. `permissions.approval_grant` exists and nothing calls it. This is the plan, not a defect, but it means milestone 1 must not be deployed where the definitions directory is shared, and WP-18 carries the whole of this answer.

**Can an agent run SQL outside approved join paths?** Not through Makna: no input accepts SQL text (the request model forbids unknown fields, and an `sql` field was refused with `INVALID_INPUT`), every join in generated SQL comes from a declared relationship, and filter values go in as literals. Makna cannot stop an agent from running other SQL with its own access; that is D-21. One gap inside Makna's own guarantee: a declared relationship is not checked to be many-to-one (C-6).

## 4. Requirements marked done, checked against code and tests

"Checked" means I ran the tests that cover it and read the code or probed the behaviour named.

| ID | State | How checked |
|---|---|---|
| NFR-4 | Met | No value in `deploy/.env.example`; unit tests on tracked `.env` files and missing variables pass. |
| NFR-5 | Met for what exists | Three tiers, lint, types and evals run in CI; the agent eval is not built yet. |
| DAT-1, DAT-2 | Met | Generator tests (determinism, integrity, quirks) pass; the engine tier ran on the generated data. |
| DAT-3 | Met | 23 approved metrics, one draft, one deprecated. |
| SEM-1, SEM-2 | Met, with C-6 | Models, schema, rules and example requests exist and are tested. |
| SEM-3 | Partly | Files validate against the published schema; the build does not enforce the cross-file rules (C-1); the git write path is WP-18. |
| SEM-4 | Met for the check | R01 to R05 tested; zero open issues on the sample. The report page is WP-19C. |
| CON-1, CON-3 | Met | File connector, sync and summary tested; 9 datasets and 92 columns synced here. |
| CON-4 | Met | Engine tier passed; `makna catalog compare` green in CI. |
| SRC-1 | Partly | Hybrid search with fusion works; ranking across kinds is poor (C-3) and unmeasured. |
| SRC-2 | Met on the golden set only | "NPF" and "kredit macet" find `npf_ratio`; paraphrases do not carry over (C-2). |
| SRC-3 | Met as written, with C-3 | Deprecated hidden by default, drafts hidden from agents; a draft or deprecated hit that is shown sorts below everything. |
| SRC-4 | Met | Four providers, contract tests, the local model runs offline in CI. |
| QRY-1 | Met, with C-5, C-6, C-9 | Parts A and B of the compile eval pass on PostgreSQL; joins come only from declared relationships. |
| QRY-2 | Met for marked columns | See section 3; limits in C-4. |
| QRY-3 | Met, with C-8 | SQL, columns, explanation and warnings are returned. |
| GOV-2 | Met for the log | One row per call, redaction as specified (probed). The page is WP-19C. |
| WEB-5 | Partly, as planned | Users, Argon2 hashes, tokens; sign-in is WP-16. |
| MCP-1, MCP-2 | Met, with C-12 | Four read-only tools; live server probed over HTTP. |
| EVL-1 | Met as written | 51 questions with reference SQL and frozen results, 6 `clarify`, 6 `no_metric`, 3 `refuse`; the second review is recorded in pull request 11. Its independence from the definitions is the problem of C-2. |
| EVL-2, EVL-3, EVL-5 | Met | Scores above; one command; runs in CI. |
| NFR-1 | Partly, as planned | The stack starts in CI; it has no web app yet. Not run here (no Docker). |
| NFR-2 | Met | 12 ms here, 21 ms in CI, on 194 documents. |
| NFR-3 | Met in CI | The offline stack step is green. Not run here. |

## 5. Does the implementation follow the architecture?

Yes, closely. The dependency rule is enforced by a test and holds. The compiler is pure and builds every statement as a syntax tree. Search is one statement. The three interfaces share one service layer, of which two (web, resolver) are still empty packages, as planned.

Differences that are improvements, all recorded under "Details settled" in the architecture: the MCP server writes its tool schemas out and refuses unknown fields instead of dropping them; the endpoint is stateless and answers `POST` only; the catalog check is per table (D-30); R05 turns an internal governance failure into a structured refusal; the function and cast allow-list; the governance modes.

Differences that are problems: none in the code against the text. The problems are in the design text itself, which the code follows faithfully: the weights of section 8.2 (C-3), the unchecked many-to-one assumption of section 7.3 (C-6), and the ratio rule of section 7.4 (open question 4).

One thing to watch for WP-18: `index_object` leaves the stored available dimensions as they are and marks every stored fact stale, so after the first approval made through the application every `get_definition` computes its dimensions from the snapshot until someone runs a full `makna index build`, and the compile eval refuses to start in between. That is correct and safe, but the lifecycle should end each action with the facts fresh.

## 6. The twelve open questions

1. **Should a measure take a list of rules?** Yes. Today five metric files repeat `{rule: contract_snapshot.in_book}` because their measures already use their one rule, and each measure's description has to say "counts the write-off position too; the metric applies `in_book` on top". The measure alone is therefore not the business number, and a new metric that forgets the filter is silently wrong. A list (`rules: [non_performing, in_book]`, all must hold) is a small change to the schema, V18, the planner and the `uses_rule` edges. Do it before WP-18 and WP-19 fix the schema in an editor.
2. **Should the group size count the person?** In the sample the question does not arise: every personal dimension sits on `customer`, and the SQL counts `COUNT(DISTINCT customer_id)`. It matters when an adopter puts a personal dimension on an entity that is not the person. Then yes, it should count the person, and the cheapest form is an optional key on the entity that names the person column of its own table (for example `cust_id` on applications), counted instead of the primary key. No join is needed. It is not blocking, but nothing tests the case today.
3. **Do documents for personal dimensions stay searchable?** Yes, that is right. An agent must be able to learn that `customer.full_name` exists and is never available, or it cannot explain a refusal. I confirmed that the document and the definition carry no values. Keep the known limit in view: a dimension the catalog newly tags personal keeps its values in the search document until the next build.
4. **Is the ratio rule too strict?** It is too strict and also not consistent. `collection_rate` cannot be cut by any `contract` dimension (status, tenor), because its inputs enter `contract` through `payment.contract` and `installment.contract`. Yet it can be cut by branch, product and customer dimensions, which are reached through that same contract. If the contract rows could differ, so could everything behind them. No rule on the shape of the graph can tell "the contract this row belongs to" (one meaning, two tables) from "booking branch against receiving branch" (two meanings). A curator has to say it. Smallest change: compare the relationship's own name and target, not the name with its entity in front, and write down that a relationship's name is its role. `payment.contract` and `installment.contract` then agree, `booking_branch` and `servicing_branch` do not. It becomes a naming rule that approval reviews.
5. **Is "approved definitions are not hostile" acceptable, and what must the agent runner do?** Acceptable, on two conditions. First, be clear about where the rule can bind: under D-21 an agent's own database tool shows it the engine's error text, and Makna cannot prevent that, so "do not pass error text on" applies only to runners the adopter controls. The eval harness (WP-22B) must return the SQLSTATE and error class only, as `evals/sqlrun.py` does. Second, narrow the accidental case: a dimension that reads a personal column should not be allowed a cast that can fail on data (the allow-list checks the target type only, so `CAST(national_id AS INTEGER)` passes V05), and a `bucket` mask casts its column to a floating type, which fails with the value in the message when a numeric dimension is declared over a text column. The catalog's `data_type` can catch the second as a reference issue.
6. **Is documenting the differencing limit enough for this phase?** Yes, if the texts stop short of promising more than the rule does (C-4). Differencing took me two requests. The larger gap is the one beside it: person-level groups with no personal dimension selected.
7. **Is the retrieval result trustworthy, and should the model or the gate change?** Not as a measure of how people will ask. Change the gate first, then the weights, then decide the model: see C-2. The held-out numbers suggest the current model is worth more than weight 0.25, so the model decision should wait for a fair measurement.
8. **Should a deprecated hit the caller asked for sort below every approved hit?** No. See C-3: with weight 0.5 it does not sort lower, it disappears from the first page. Someone who ticks "include deprecated" is usually looking for that object.
9. **Is parsing with the default dialect a risk?** Yes, a moderate one: C-5.
10. **Should `brief` be the default for agents?** Not yet. Measured over the 23 approved metrics: `full` is 4,419 to 10,875 characters (mean 8,887), `brief` 2,462 to 5,161 (mean 3,993). In the `full` form of `npf_ratio`, three quarters of the text is the dimension list. `brief` drops the allowed values, which an agent needs to write a filter without a failed attempt. Keep `full` as the default until the agent eval can compare the two on accuracy and tokens; a middle form (values, no descriptions) is the likely winner.
11. **Is no throttle on token checks acceptable?** Yes for tokens. A token is 32 random bytes, a failed check is one indexed statement that writes nothing, and no guess can succeed. What remains is load from a flood of requests, which belongs to the reverse proxy in front of the application; say so in `deploy/README.md`. Passwords are different and WP-16 must throttle them.
12. **Messages that repeat what the caller typed, against "details never hold values": which gives way?** The documentation, narrowly. A message may repeat the caller's own input, cut to a fixed length; it never repeats a value for a personal dimension (already so, the code says "the value"), and it never goes to a log (already so: the log row of a refused call holds the error code, and the value is `[REDACTED]` unless the dimension is known and not personal; I checked the stored rows). What must never appear in a message or in details is a value read from data, and no code path can put one there because Makna reads no rows.

## 7. Where the sources disagree

- `docs/DECISIONS.md` D-4 still says agents can "search, read, compile, optionally run". D-21 dropped running.
- `docs/EVALS.md` section 3 says the retrieval eval calls the search service; as built it calls `HybridSearch` (recorded in the same section, and a test keeps the two equal).
- `docs/ARCHITECTURE.md` section 12: the `makna_search` description mentions `ambiguous_between`, and the default kinds include `rule` and `domain`, which `kinds` does not accept (both recorded there as known disagreements).
- `docs/ARCHITECTURE.md` section 5.2 says deprecated objects are refused by the compiler; a deprecated entity reached by a join is not (C-10).
- `docs/ARCHITECTURE.md` section 7.3 states as a fact that a forward join cannot multiply rows; nothing enforces what makes it true (C-6).
- `tests/unit/golden/make_governance_golden.py` gives two instructions that cannot both be followed (C-11).

## 8. What I did not check

- The development stack itself (no Docker). I relied on the green CI run for NFR-1, NFR-3, the roles of the two databases, and the untouched three-part table names.
- Another embedding model (only the default is cached offline).
- Behaviour under load: many concurrent MCP calls against the database pool, and very large request bodies on `/mcp`. Unconfirmed either way.
- The governance modes `custom` and `off` beyond their tests; every probe here ran under `strict`.
- The stdio bridge beyond its tests.

## 9. Files of this review

- `docs/reviews/core-complete.md`: this report.
- `docs/reviews/core-complete-probes/`: four scripts that reproduce the evidence of C-2, C-3, C-4 and C-5 (`heldout_retrieval.py`, `search_ranking.py`, `privacy.py`, `expression_dialect.py`). They read the same settings as the evals and use the synthetic data only.
- `tests/unit/test_review_core_complete.py`, `tests/integration/test_review_core_complete.py`: six tests, marked as expected failures, for C-1, C-3, C-5 (two), C-6 and C-9.
