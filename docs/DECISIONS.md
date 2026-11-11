# Decisions and Open Questions

Status: reviewed (2026-11-11). The independent review is `docs/reviews/design.md`; decisions D-10 to D-15 and the revisions marked "Revised after review" came out of it.

## Decided by the project owner

These are settled. Do not reopen them without new information.

| ID | Decision | Date |
|---|---|---|
| D-1 | The platform is open and company-agnostic. It ships with synthetic data only. Company-specific connectors and definitions are added later by the adopting organization, outside this repository. | 2026-11-11 |
| D-2 | The platform layers on top of a data catalog. The catalog stays the source of truth for physical metadata. | 2026-11-11 |
| D-3 | Curators and business users are equally important users of the MVP web app. | 2026-11-11 |
| D-4 | Agents get read-only access in the MVP (search, read, compile, optionally run). Agents never approve definitions. | 2026-11-11 |
| D-5 | Draft definitions, including AI-drafted ones, are never official until an authorized owner approves them. | 2026-11-11 |
| D-6 | Semantic definitions are stored as YAML under version control. The database is an index. | 2026-11-11 |
| D-7 | ~~Trino is the reference query engine.~~ Superseded by D-22: PostgreSQL is the MVP target engine and DuckDB the unit-test engine. | 2026-11-11 |
| D-8 | Python is the primary language across the whole platform: backend, connectors, query compiler, MCP server, evals, data generation, and the web app's server side. JavaScript or TypeScript is allowed only for browser-side pieces that Python cannot provide (for example a code editor widget or a diagram), kept small and without a separate frontend build where possible. Reason: the owner and the teams most likely to adopt and maintain the platform work in Python. | 2026-11-11 |
| D-9 | The project is named Makna. The repository is `makna`, the Python distribution name is `makna-sl`, and the import package is `makna`. Publishing to PyPI is not decided yet, so do not set up publishing. | 2026-11-11 |
| D-10 | Metric definitions: git stays the source of truth (D-6, DR-4). Publishing approved metrics back to a data catalog (for example as OpenMetadata Metric entities) comes after the MVP. Until then the catalog is read-only to Makna. | 2026-11-11 |
| D-11 | Approval is two-person by default: the author of a change cannot approve it (rule in `docs/ARCHITECTURE.md` section 11). `MAKNA_ALLOW_SELF_APPROVAL=true` turns this off for single-user installs. The default is `false`, including in the dev stack. | 2026-11-11 |
| D-12 | The ninth sample table, `contract_monthly_snapshots`, is accepted as part of DAT-1 (`docs/SAMPLE_DOMAIN.md` section 3.9). | 2026-11-11 |
| D-13 | The EVL-4 agent comparison uses Gemini through a Google AI Studio API key with a limited rate. The harness gets a Gemini adapter behind its provider-neutral interface, a configurable rate limiter, resumable runs, and a quick mode (20 questions, 1 run per arm) that runs before the full run. The key comes from the environment variable `GEMINI_API_KEY` and is never committed. The owner provides it when WP-22B is reached. Details in `docs/EVALS.md` section 5.5. | 2026-11-11 |
| D-14 | Part of the synthetic catalog metadata is deliberately poor: some tables with missing or vague descriptions, abbreviated column names, and mixed Indonesian and English naming, so that search and agent evals reflect real catalogs rather than tidy ones. Specified in `docs/SAMPLE_DOMAIN.md` section 6 (quirk Q11); built in WP-02 and WP-06. | 2026-11-11 |
| D-15 | Coding sessions may not have Docker. Agents test locally on DuckDB (unit tier) and treat results on the target engine (PostgreSQL since D-22; Trino when this was decided) as proven only when CI on GitHub Actions passes, unless a PostgreSQL is available in the session. Rules in `docs/PLAN.md` ("How to use this plan") and `CLAUDE.md`. | 2026-11-11 |
| D-16 | The owner compared the design with an adopter's catalog requirements, which make the catalog the long-term home of metric definitions. D-10 stands for the MVP: definitions live in git, and publishing approved definitions to the catalog is the later step that makes the catalog their home for readers. | 2026-11-11 |
| D-17 | Business rules are first-class: an entity may declare named rules (a boolean condition written once, such as "more than 90 days past due"). Measures and metric filters reference them, and a change to a rule shows every measure and metric that uses it. Linking rules to pipeline columns (lineage) comes later. | 2026-11-11 |
| D-18 | Every approved metric carries at least one verified example: a question and the structured request that answers it. Every example request must compile (V19). These replace verified question-and-SQL pairs: with a compiler, the request is what an agent needs, and the SQL is produced from it. | 2026-11-11 |
| D-19 | Metrics, entities, and glossary terms must have at least one English and one Indonesian synonym (SEM-2). Synonyms stay optional on dimensions, rules, and domains. | 2026-11-11 |
| D-20 | A change that loosens sensitivity needs approval from the governance team (configured; default `data-governance`), in addition to the normal approval rules. Loosening means: an object, dimension, or rule declared at a less strict level than its official version; masking removed or made finer; or a new object with a `personal` dimension. Business meaning stays with the owner team. | 2026-11-11 |
| D-21 | **Makna compiles only.** It never runs a caller's query. Agents and people run the compiled SQL with their own tools and their own engine access. Makna's join rules and masking hold for the SQL it produces. Access control on the engine is the protection against SQL that Makna did not produce. QRY-4 and the run tool are dropped; CON-4 keeps only what Makna needs for itself (introspection and the freshness check, with a read-only service account). | 2026-11-11 |
| D-22 | **Trino is put aside.** In the MVP the target engine is PostgreSQL: the dev stack's sample data and the engine tier of tests. DuckDB runs the generated SQL in unit tests. The compiler builds one dialect-neutral syntax tree and prints it for `postgres` or `duckdb`; Trino can come back later as a dialect and a connector (CON-7). Supersedes D-7. | 2026-11-11 |
| D-23 | Reranking (SRC-5) stays in phase 2. The brief is corrected to match. | 2026-11-11 |
| D-24 | GOV-2 reads "with personal data removed where it can be detected". Numbers, email addresses, phone numbers, and personal filter values are removed. A name typed into free text cannot be detected reliably, so the tool-call log is visible to approvers and admins only. | 2026-11-11 |
| D-25 | A materialization registry (which physical columns hold a metric pre-computed at which grain) is phase 2. The compiler always computes from base tables at the requested grain, which avoids averaging pre-computed ratios. | 2026-11-11 |
| D-26 | No language model runs inside Makna in the MVP: no built-in chat assistant and no LLM gateway. Agents stay read-only, so there is no feedback or write tool for them (D-4). Any MCP client, including an adopter's own catalog assistant, can use Makna's tools. | 2026-11-11 |
| D-27 | The PostgreSQL driver is `psycopg` (version 3), as `docs/ARCHITECTURE.md` section 6.3 says. The owner approved its LGPL-3.0 licence (`CLAUDE.md` asks for approval of non-permissive licences). Makna uses the library unmodified as an ordinary dependency. | 2026-11-11 |

## Proposed defaults (the design session may change these with a written reason)

| ID | Proposal | Reason |
|---|---|---|
| P-1 | Backend in Python with FastAPI. | Matches the owner's skills and the data and AI ecosystem. |
| P-2 | PostgreSQL with pgvector and built-in full-text search for the index and hybrid search. | One database to run. Good enough for thousands of objects. |
| P-3 | sqlglot for SQL generation and dialect handling. | Mature, pure Python, supports Trino. |
| P-4 | Web app rendered on the server by FastAPI with Jinja templates and HTMX for interactivity, plus small JavaScript islands only where needed (for example CodeMirror for the YAML editor). | Keeps the app in Python (D-8), one codebase, no separate frontend build. A Python-to-React framework such as Reflex is the alternative if the design session finds HTMX too limiting; record the reason if so. |
| P-5 | MCP server with the official Python MCP SDK, in the same codebase as the API. | Shares the domain logic. |
| P-6 | Docker Compose for local development. Trino with a sample catalog loaded from the synthetic data. | One command to start. |
| P-7 | Local embedding model by default (served, for example, through Ollama), with an option for hosted providers. | Self-hosted requirement. |
| P-8 | One Python project (managed with uv) with packages for the core, connectors, API, MCP server, and web app, plus top-level `semantic/` (YAML definitions), `evals/`, `data/`, and `docs/`. | Keeps contracts and tests together in one language and one dependency set. |

## Questions answered in the design session

Each question is answered by a decision record below.

| ID | Question | Answer |
|---|---|---|
| Q-1 | Build our own semantic model schema, or adopt or extend an existing format? | DR-1: our own small schema, shaped so it can be exported to Apache Ossie. |
| Q-2 | Which open-source catalog first, and what is the minimal common metadata model? | DR-2: OpenMetadata through its REST API; four models. |
| Q-3 | How is a question resolved to a metric request? Where does an LLM begin? How is ambiguity handled? | DR-3: the calling agent is the LLM; Makna is deterministic; ambiguity returns options. |
| Q-4 | How do YAML in git and web edits stay consistent? | DR-4: the application commits to a git repository; proposals are sibling files. |
| Q-5 | How are sensitivity tags and masking defined and enforced? | DR-5: four levels, masking rules on dimensions, enforced in the compiler plan and checked on the final syntax tree. |
| Q-6 | What is the minimal role and permission model? | DR-6: four roles, owner-team approval, two-person rule, read-only agent tokens. |
| Q-7 | What do the MCP tools return? | DR-7: four tools (five before D-21 dropped the run tool) with compact JSON and tested size budgets. |
| Q-8 | What is the shape of the golden set and how is an agent run scored? | DR-10: YAML questions with reference SQL; result-based scoring. |

## Status of the proposed defaults

| ID | Status after the design session |
|---|---|
| P-1 | Kept. FastAPI. |
| P-2 | Kept. Proven in `spikes/search/`. Detail added: exact vector search with no vector index in the MVP (DR-9). |
| P-3 | Kept. Proven in `spikes/compiler/`. |
| P-4 | Kept, with one detail changed: the YAML editor widget is Ace, not CodeMirror (DR-8). |
| P-5 | Kept. Note that the official SDK is now version 2 and renamed its server class (DR-7). |
| P-6 | Kept, without Trino since D-22: the dev stack is PostgreSQL with the sample data, plus the Makna application (DR-11). |
| P-7 | **Revised.** The default local model runs inside the Makna process through `fastembed`, not through a separate Ollama server. Ollama stays as an option (DR-9). |
| P-8 | Kept. Layout in `docs/ARCHITECTURE.md` section 3. |

## Decision records from the design session (2026-11-11)

Facts about outside tools were checked on 2026-11-11. "Checked" means read in this session from the named source. Where a fact comes from general knowledge and was not re-checked, the record says so.

### DR-1 (answers Q-1): Our own semantic schema, aligned with Apache Ossie for later exchange

**Options considered**

1. *Adopt Apache Ossie (formerly Open Semantic Interchange) as the storage format.* Checked in the repository `github.com/apache/ossie` at commit `08d2ad3` (2026-11-10): the project entered the Apache Incubator in June 2026 and was renamed because "OSI" collides with the Open Source Initiative. The only released specification is 0.1.1 (2025-12-11). The current specification is `0.3.0.dev0`, marked "unreleased and mutable; do not depend on this version in production", with breaking changes merged on 2026-09-16, 2026-11-09, and 2026-11-10. Its core model has datasets, fields, relationships (many-to-one), metrics as SQL expression strings, and `ai_context` (instructions, synonyms, examples). It has no owner, lifecycle state, or sensitivity fields, and the specification states that a metric expression cannot refer to another metric. We would lose: ratio and derived metrics built from governed parts, balance measures, structured measures the compiler can aggregate safely, and every governance field (which would all go into vendor extensions).
2. *Adopt the MetricFlow format and compiler.* Checked on PyPI: `metricflow` 0.213.0 (2026-09-10) is Apache-2.0 and depends on `sqlglot`; it lists a Trino test environment. The dbt Labs announcement confirms the Apache-2.0 licence. The usable packaging, `dbt-metricflow` 0.15.0, requires `dbt-core`. The format is rich (semantic models, measures, entities, simple, ratio, derived, and cumulative metrics). We would lose: independence from a dbt project (not re-checked: MetricFlow reads a dbt semantic manifest); control of the compile step where masking, refusals, approved-only rules, and the explanation (QRY-2, QRY-3) must live; and first-class governance fields (they would go into free-form `meta`).
3. *Adopt Malloy.* Not re-checked in this session. From general knowledge: Malloy is its own modeling and query language with a TypeScript toolchain, not YAML that a governance team edits and a JSON Schema validates. It conflicts with D-6 and D-8.
4. *Our own small schema that borrows the proven concepts.*

**Choice.** Option 4: `semantic/schema/makna.schema.json`, version `makna/v1`.

**Reason.** Governance is the point of this platform (owner, state, sensitivity, masking, approved join paths), and none of the existing formats carries it as first-class data. The two best candidates are either unreleased and changing weekly (Ossie) or tied to another tool's project structure and compiler (MetricFlow). The concepts are borrowed deliberately so that exchange is cheap: entity with fields and many-to-one relationships (Ossie datasets and relationships), synonyms and example questions (Ossie `ai_context`), measures with an aggregation and one time dimension, simple, ratio, and derived metrics, and non-additive balances (MetricFlow). SEM-5 (phase 2) should target Apache Ossie export first, once 0.3.0 is released; our governance fields map to its `nonportable_extensions`.

**What we lose.** No ecosystem tooling reads our files today, and we maintain a compiler (kept small by DR-12).

Sources: `https://github.com/apache/ossie` (`core-spec/spec.yaml`, `core-spec/spec.md`, `docs/spec_changes.md`); `https://www.dremio.com/blog/apache-ossie-the-open-semantic-interchange-joins-the-apache-incubator`; `https://www.snowflake.com/en/blog/open-semantic-interchanges-specs-finalized/`; `https://pypi.org/project/metricflow/`; `https://pypi.org/project/dbt-metricflow/`; `https://www.getdbt.com/blog/open-source-metricflow-governed-metrics`.

### DR-2 (answers Q-2): OpenMetadata first, through its REST API; a four-model common metadata model

**Options considered.** OpenMetadata; DataHub.

**Choice.** OpenMetadata, called over its REST API with `httpx`. The common model is `CatalogDataset`, `CatalogColumn`, `CatalogOwner`, and `CatalogGlossaryTerm` (`docs/ARCHITECTURE.md` section 6.1). The default development stack uses the file-based connector (CON-3); OpenMetadata is an optional extra stack.

**Reason.** Checked: OpenMetadata 2.0.7 was released on 2026-10-27; it has a governed Metric entity and a glossary with documented REST endpoints (`/v1/metrics`, and so on), which matters because the adopter wants its catalog to be a home for metric definitions; the server's repository licence is Apache-2.0. Not re-checked, from general knowledge: OpenMetadata needs a database and a search engine to run, while DataHub also needs Kafka, so OpenMetadata is the lighter stand-in. DataHub's Python package (`acryl-datahub` 1.7.0.19) is Apache-2.0 and remains a reasonable second connector.

**Important constraint.** Checked on PyPI: the OpenMetadata Python SDK (`openmetadata-ingestion` 2.0.7.2) is published under the "Collate Community License Agreement", which is not a standard permissive licence. `CLAUDE.md` requires the owner's approval for such a dependency. The connector therefore must not import the SDK; it calls the REST API directly. See `docs/notes/openmetadata-sdk-license.md`.

**The common model is minimal on purpose.** It holds exactly what CON-1 lists and what the reference check (SEM-4) and the sensitivity rules (DR-5) need. The key is `engine_fqn`, the table name as the query engine sees it, because the semantic layer must join catalog metadata to SQL. Catalog-side metrics and lineage are not in the model yet.

Sources: `https://docs.open-metadata.org/latest/releases/all-releases`; `https://docs.open-metadata.org/v2.0.x/api-reference/governance/metrics`; `https://pypi.org/project/openmetadata-ingestion/`; `https://github.com/open-metadata/OpenMetadata` (`LICENSE`); `https://pypi.org/project/acryl-datahub/`.

### DR-3 (answers Q-3): The calling agent is the language model; Makna itself is deterministic; ambiguity returns options

**Options considered**

1. A language model inside Makna turns the question into a request.
2. Makna is deterministic: search, validation, and compilation. The calling agent, which is already a language model, chooses the metric and builds the structured request. A rule-based resolver serves the test bench.
3. Rules only, with no structured request exposed to agents.

**Choice.** Option 2. Details in `docs/ARCHITECTURE.md` sections 8.3 and 8.4.

**Reason.** NFR-3 forbids a required outside service, and a local model good enough for this would dominate the deployment. Every consumer of the MCP server is already a language model, so a second one inside Makna adds cost, latency, and a second place where answers can vary. Keeping Makna deterministic also makes the evals meaningful: retrieval and compile scores do not change from run to run. The line is therefore: **words to candidates is search (deterministic); candidates to a request is the agent (or the rule-based resolver in the test bench); request to SQL is the compiler (deterministic and strict).**

**Ambiguity.** Return the options, never pick silently: search flags `ambiguous_between`, and the resolver returns `status: ambiguous` with candidates. The exception is where governance has already decided (for example which branch link a metric uses): then Makna applies the decision and says so in the explanation. "Nothing matches" is reported as low confidence with explicit guidance, because the search spike showed that scores alone cannot decide it (`spikes/search/README.md`).

**What we lose.** The test bench resolves only what rules can: declared synonyms, declared values, and a fixed set of time phrases. An optional language-model resolver can be added behind the same interface later.

### DR-4 (answers Q-4): The application commits to a git repository; proposals are sibling files

**Options considered**

1. The application owns a database store and exports to git. This contradicts D-6 in spirit: the database would be the source of truth and git a copy that can drift.
2. The application opens pull requests on a git host. This ties the MVP to one host's API and makes "approve in the web app" depend on an outside service (against NFR-3).
3. The application commits to a local git repository; one branch per change request. Git-native, but merges and conflicts in application code are a large surface for a small team.
4. The application commits to a local git repository with one branch. Lifecycle state is in the file. A pending change to an approved object is a sibling file `<name>.draft.yaml`.

**Choice.** Option 4. Details in `docs/ARCHITECTURE.md` sections 5.2 and 10.

**Reason.** Everything, including drafts, is YAML under version control (D-6), and the whole state is visible in a plain checkout: the index can be rebuilt from `HEAD` and the audit table. The approved version keeps being served while a change is in review, because the two are different files. Every lifecycle action is one commit with the user as author and the reason as message, so git history is the audit trail (GOV-1) and the database table is a queryable copy. **Revised after review (R-1).** Only commits the application makes can make a definition official. The application records an intent row in `audit_event` before it commits, and trust comes from that table, not from commit trailers, which anyone can write. Any other commit (a merged pull request, a hand edit, a coding agent's commit) is indexed as a proposal in review and is never served as approved. Organizations that prefer pull requests (as with their Feast repository) can still use them on the same repository, but the merged result needs one approval in Makna.

**How consistency is kept.** One writer at a time (a PostgreSQL advisory lock). Record the intent in the audit table, commit, then update the index and complete the audit row in one transaction. If that transaction fails, `makna index sync` completes the work for each commit after `last_indexed_commit` that matches an intent row. Git access goes through the `git` command line behind one wrapper class. `pygit2` was rejected because it is GPL-2.0 with a linking exception (checked on PyPI), which needs the owner's approval.

### DR-5 (answers Q-5): Four sensitivity levels; masking rules on dimensions; enforced in the compiler and re-checked on the final syntax tree

**Choice.** Details in `docs/ARCHITECTURE.md` section 7.6.

- Levels: `public`, `internal`, `confidential`, `personal`. Every object has one; dimensions may set their own.
- The effective level of a dimension is the stricter of the YAML level and the level derived from catalog tags on the columns it reads (mapping in configuration).
- A `personal` dimension is returned only through its `masking` rule (`redact`, `truncate_date`, `bucket`); with no rule it cannot be selected; it can never be filtered on; aggregates over it are limited to counts.
- **Revised after review (R-4, R-9).** `partial` masking ("last four digits") is removed: as a grouping key it returned one row per customer on the spike data. When a request selects a personal dimension, groups built from fewer than `min_group_size` records (default 10) of the entity holding it are dropped. A personal dimension cannot be a measure's official date or list `values` (V16, V17), and the final check accepts only `COUNT` and `COUNT(DISTINCT ...)` around personal columns.
- No role can unmask in the MVP, and no interface accepts SQL text.
- Enforcement is in two places: the planner refuses or substitutes the masked expression, and a final pass over the generated syntax tree fails if a personal column appears outside a mask or a count.

**Reason.** The catalog is the source of truth for tags (D-2), so taking the stricter of the two means a newly tagged column is protected at the next sync without waiting for a curator. Refusing filters on personal fields closes the "look up one person" path, which matters more under a personal data protection law than convenience. Two enforcement points mean a bug in SQL generation cannot leak a raw column. The spike in `spikes/compiler/` exercised YAML-declared sensitivity, `truncate_date` masking, refusals of unmasked selection and of filters, and the final check. It did not exercise catalog-derived sensitivity, `redact`, `bucket`, the counts-only rule, or the minimum group size; WP-10 tests them.

**Options rejected.** Hash masking (an unsalted hash of a phone or identity number can be reversed by guessing; a salted one would put the salt in SQL text returned to agents). Relying on engine-side masking only (not every engine has it, and the agent would still see the column exists and try it). Row-level policies are phase 2 (QRY-5).

**What this does not do.** It applies the minimum group size only when a personal dimension is selected. Small groups formed only from non-personal dimensions (for example one contract's amount by branch and day) are not suppressed; a general rule for that is a phase 2 candidate.

### DR-6 (answers Q-6): Four roles, owner-team approval, a two-person rule, and read-only agent tokens

**Choice.** The matrix in `docs/ARCHITECTURE.md` section 11. Roles are `viewer`, `curator`, `approver`, `admin`. A user has one role and a list of teams. An object may be approved by an approver or admin, or by a curator whose teams include the object's owner team. No author of any commit on a proposal can approve it, unless `MAKNA_ALLOW_SELF_APPROVAL=true` (owner decision D-11; default `false`). **Revised after review (R-6):** approval rights follow the official file's owner team, not the proposal's; a new object's owner team must be one of the proposer's teams; a change of owner team needs an approver or admin. API tokens act as a viewer at most and can never write. **Revised after review (R-2, R-3):** every MCP channel, stdio included, authenticates with a token (the stdio command is a bridge to the HTTP endpoint with no database settings), and agents never get Makna's database settings. (The R-3 fix that stored engine credentials with tokens was superseded by D-21: Makna runs no queries, so it holds no engine credentials for callers.) **D-20** adds one more approval rule: a change that loosens sensitivity needs a member of the governance team.

**Reason.** This is the smallest model that satisfies "only owners or approvers can approve" (WEB-3) and D-4 and D-5. The two-person rule is the control a regulated lender expects; the switch exists so a single-developer install is not locked out. Per-domain roles and groups from single sign-on are phase 2.

### DR-7 (answers Q-7): Four MCP tools with compact JSON, tested size budgets, and actionable errors

**Choice.** `makna_search`, `makna_get_definition`, `makna_list_related`, and `makna_compile_query`. **Revised by D-21:** the fifth tool, `makna_run_query`, is dropped; agents run the compiled SQL with their own tools. Exact inputs, outputs, and descriptions are in `docs/ARCHITECTURE.md` section 12.

**Reason.** Search returns five brief hits, not full definitions, so the common first call is small (budget 2,500 characters); the agent pays for a full definition only for the candidate it picks (budget 6,000 characters). The definition lists the exact dimension references, values, and grains the compiler accepts, so the agent can build a valid request on the first try. Errors list what is allowed, so a wrong request is repaired in one more call. Budgets are enforced by tests so that later changes cannot bloat the responses.

**Fact that affects the build.** Checked by installing it: the official Python SDK is `mcp` 2.3.0 (MIT). In version 2 the high-level server class is `mcp.server.mcpserver.MCPServer`; importing `mcp.server.fastmcp` raises an error. Most examples online still show the version 1 name. See `docs/notes/mcp-sdk-2-renamed-fastmcp.md`.

### DR-8 (refines P-4): Server-rendered pages with HTMX; Ace for the YAML editor; no build step

**Choice.** Keep P-4. Vendor three browser assets as static files: `htmx.org` 2.0.11 (0BSD), `ace-builds` 1.45.2 (BSD-3-Clause), and Pico CSS 2.1.1 (MIT) (versions and licences checked on the npm registry).

**Reason.** The pages are documents, lists, forms, and one editor; HTMX covers the partial updates they need (search as you type, validate without saving, approve in place). Reflex was not needed: nothing in the MVP requires client-side state. CodeMirror 6 is published as modules that need a bundler, which would add the Node.js build that D-8 and P-4 want to avoid; Ace ships prebuilt files with a YAML mode.

**Scope of the editor.** The MVP editor is YAML text with live validation, templates, and a difference view. Form-based editing for non-technical curators is a phase 2 candidate.

### DR-9 (revises P-7): The default embedding model runs in-process with fastembed; exact vector search

**Options considered.** A model served by Ollama (the proposal); a model run in the Makna process with `fastembed` (ONNX on CPU); `sentence-transformers` with PyTorch.

**Choice.** `fastembed` in-process with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions) as the default provider. Ollama and OpenAI-compatible endpoints are alternative providers behind the same interface. A deterministic `hash` provider exists for tests.

**Reason.** One container and one network hop fewer in every deployment, and no model server to operate, while keeping the self-hosted default (SRC-4, NFR-3). Checked in the installed package: `fastembed` 0.9.1 is Apache-2.0 and its supported list includes that multilingual model (Apache-2.0, about 0.22 GB). PyTorch would add gigabytes to the image.

**Not proven.** Model downloads were blocked in the design environment, so the quality of this model on Indonesian finance vocabulary was not measured. WP-07 and WP-15 must measure it and may switch the default (for example to `intfloat/multilingual-e5-large`, also on the supported list, at about 2.2 GB). The provider interface makes this a configuration change.

**Vector index.** None in the MVP: exact cosine search over a few thousand rows takes milliseconds and avoids approximate-search recall loss and a fixed vector size. Add an HNSW index when a deployment passes roughly 50,000 documents.

### DR-10 (answers Q-8): Golden questions as YAML with reference SQL; agents scored on results, not on wording

**Choice.** `docs/EVALS.md`. Each question has an expected outcome (`answer`, `clarify`, `no_metric`, `refuse`), the expected metric and request, and a hand-written reference SQL whose result on the seeded data is the expected result. Retrieval is scored with recall at 3 and mean reciprocal rank; compilation by comparing the compiled query's result with the reference result; the end-to-end comparison by whether the agent's final structured answer matches the expected result or the expected behaviour.

**Reason.** Result-based scoring is objective and the same for both arms of the comparison, which a wording-based or judge-model score would not be. Reference SQL written independently of the compiler is what gives the compile eval its value. A fixed "today" (2026-07-01) keeps relative time deterministic.

### DR-11 (refines P-6): The sample data lives in PostgreSQL; DuckDB runs the SQL in unit tests

**Revised by D-22.** Trino is no longer in the stack. PostgreSQL is the target engine: the compiler prints SQL for the `postgres` dialect, and the engine tier and the evals run it on the `lending` database directly. The text below is the original record, kept for its reasoning; read "Trino" in it as "the target engine".

**Choice.** The dev stack loads the synthetic data into a `lending` database on the same PostgreSQL server and exposes it through Trino's PostgreSQL connector as catalog `lending`. **Revised after review (R-2):** the two databases have separate roles; the application's role cannot connect to `lending`, and Trino reads it through a read-only role. Unit tests print the compiler's syntax tree for DuckDB and run it in-process.

**Reason.** No extra storage service, durable data, fast load. The compiler builds a dialect-neutral sqlglot tree, so a DuckDB run costs nothing extra and gives fast correctness tests; the Trino tier still runs every approved metric on the real engine. DuckDB is a test tool, not a supported engine (CON-7 stays in phase 3).

**Not checked (no longer relevant after D-22).** The current Trino release number could not be read in the design environment.

### DR-12: Our own compiler on sqlglot, with "aggregate each input separately, then combine"

**Options considered.** Use MetricFlow as the compiler; write a small compiler on sqlglot (P-3).

**Choice.** A small compiler on sqlglot (checked: 30.31.0, MIT). Design in `docs/ARCHITECTURE.md` section 7.

**Reason.** The compile step is where Makna's governance must act (DR-5), where the explanation is produced (QRY-3), and where errors must be shaped for agents (DR-7). The scope is bounded on purpose: one metric per request; joins only from "many" to "one"; three metric types; one extra rule for balances. `spikes/compiler/` implements all of it in about 300 lines and matches hand-written reference queries on five cases, including a ratio across two fact tables.

**What we lose.** Features MetricFlow has and we do not: cumulative and time-offset metrics, conversion metrics, several metrics in one request, and joins from "one" to "many". Add them only when a real metric needs them.

### DR-13: View of the other tools named in `docs/LANDSCAPE.md`

None of these is adopted as a dependency. This record states why, so the question is not reopened without new information.

| Tool | What was checked on 2026-11-11 | View |
|---|---|---|
| OrionBelt Semantic Layer | PyPI: `orionbelt-semantic-layer` 2.36.0 (2026-10-26), Python 3.12 or newer, built on sqlglot, FastAPI, and YAML models; licence **BUSL-1.1**. | The closest existing project in shape (YAML models compiled to SQL for agents). It cannot be a dependency: the Business Source License is not permissive (`CLAUDE.md`), and it does not carry the review workflow, catalog layering, or masking rules that are Makna's reason to exist. Useful as a reference design to read, not to copy code from. |
| Open Knowledge Format (OKF) | Google Cloud blog of 2026-06-12: version 0.1; a bundle is a directory of Markdown files with YAML front matter, one concept per file; the only required field is `type`; the licence is not stated in the article. | A format for agent-readable knowledge documents, not a semantic model: it has no schema for measures, dimensions, or joins, so nothing can be compiled from it. It is a possible **export** in a later phase (one Markdown concept per approved metric) for agents that read OKF bundles. Not a storage format. |
| Cube | Not re-checked in this session. | From general knowledge: a separate server with its own data model, caching layer, and JavaScript-centred modeling. Adopting it would replace Makna's core and conflict with D-8. Not used. |
| MCP Toolbox for Databases | Not re-checked in this session. | From general knowledge: it exposes predefined SQL statements as MCP tools. Makna's run path is "compile a governed request, then execute", so predefined statements do not fit. An agent that also needs free-form SQL can use the Toolbox next to Makna; Makna does not depend on it. |
| DataHub | PyPI: `acryl-datahub` 1.7.0.19, Apache-2.0. | A sound second catalog connector after OpenMetadata (DR-2). |

Sources: `https://pypi.org/project/orionbelt-semantic-layer/`; `https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing`; `https://pypi.org/project/acryl-datahub/`.
