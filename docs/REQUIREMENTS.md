# Requirements

Status: DRAFT for review (2026-11-11). IDs are stable; reference them in commits and plans.

Priority: **MVP** (phase 1), **P2** (phase 2), **P3** (phase 3).

## 1. Connectors

| ID | Requirement | Phase |
|---|---|---|
| CON-1 | A catalog connector interface that pulls datasets, columns, types, descriptions, owners, tags, and glossary terms into one internal metadata model. | MVP |
| CON-2 | A first catalog connector for one open-source catalog (OpenMetadata or DataHub, chosen in the design session). | MVP |
| CON-3 | A file-based catalog connector (YAML or JSON export), so any in-house catalog can integrate by exporting before a native connector exists. | MVP |
| CON-4 | An engine connector for PostgreSQL: schema introspection and the freshness check, with a read-only service account. Makna does not run callers' queries (D-21, D-22). | MVP |
| CON-5 | Incremental sync with change detection, and a clear report of what changed since the last sync. | P2 |
| CON-6 | Lineage import where the catalog provides it. | P3 |
| CON-7 | Additional engines behind the same interface (for example Trino, Hologres). | P3 |

## 2. Semantic model

| ID | Requirement | Phase |
|---|---|---|
| SEM-1 | Entities, dimensions, measures, metrics (including ratios and derived metrics), time grains, and join paths; named business rules (D-17) and verified example requests per metric (D-18). | MVP |
| SEM-2 | Every object has a plain-language description, synonyms, owner, lifecycle state, and sensitivity level. | MVP |
| SEM-3 | Definitions live as YAML files under version control, validated against a published schema. The database is an index, not the source of truth. | MVP |
| SEM-4 | Every semantic object references the physical catalog objects it depends on. Broken references are reported after each sync. | MVP |
| SEM-5 | Import from and export to at least one open format (decided in the design session). | P2 |

## 3. Search

| ID | Requirement | Phase |
|---|---|---|
| SRC-1 | Hybrid search: keyword plus vector, with fusion, over metrics, entities, dimensions, columns, and glossary terms. | MVP |
| SRC-2 | Synonyms and Indonesian plus English queries (for example "NPF" and "kredit macet" both find the non-performing financing metric in the sample domain). | MVP |
| SRC-3 | Approved definitions rank above drafts. Deprecated objects are hidden by default. | MVP |
| SRC-4 | Pluggable embedding provider, including a local model option so no data leaves the deployment. | MVP |
| SRC-5 | Reranking stage. | P2 |

## 4. Query compiler

| ID | Requirement | Phase |
|---|---|---|
| QRY-1 | Compile a request (metric, dimensions, filters, time range, grain) into SQL for the target engine (PostgreSQL in the MVP; DuckDB in tests; D-22) using only approved join paths. | MVP |
| QRY-2 | Apply masking for sensitive fields and refuse requests that would expose raw personal data. | MVP |
| QRY-3 | Return the SQL and an explanation of how it was built, so the caller can show its work. | MVP |
| QRY-4 | ~~Optional execution through the engine connector with the caller's credentials.~~ Dropped: Makna compiles only, and callers run the SQL with their own tools (D-21). | — |
| QRY-5 | Row-level access policies. | P2 |

## 5. Agent interface (MCP)

| ID | Requirement | Phase |
|---|---|---|
| MCP-1 | Read-only tools: search, get definition, list related objects, and compile query (no run tool, D-21). | MVP |
| MCP-2 | Tool descriptions written for agents: when to use each tool, what it returns, and what to do when nothing matches. | MVP |
| MCP-3 | Works with any MCP client. Tested with at least two different clients or frameworks. | MVP |
| MCP-4 | An HTTP API with the same capabilities for clients that do not speak MCP. | P2 |
| MCP-5 | Agents can propose draft definitions for human review (never approve). | P3 |

## 6. Web app

| ID | Requirement | Phase |
|---|---|---|
| WEB-1 | Business discovery: search, browse by domain, metric pages with plain-language meaning, formula, owner, state, freshness, and example questions. | MVP |
| WEB-2 | Curator editor: create and edit definitions with validation against the schema. | MVP |
| WEB-3 | Review workflow: draft, in review, approved, deprecated. Only owners or approvers can approve. Every change has an author and a reason. | MVP |
| WEB-4 | Test bench: type a business question and see the search results, the resolved metric, the compiled SQL, and (optionally) the result. | MVP |
| WEB-5 | Sign-in with roles (viewer, curator, approver, admin). Single sign-on comes later. | MVP (basic), P2 (SSO) |
| WEB-6 | Indonesian and English interface text. | P2 |
| WEB-7 | Lineage view. | P3 |

## 7. Governance and observability

| ID | Requirement | Phase |
|---|---|---|
| GOV-1 | Audit log of every definition change and every approval. | MVP |
| GOV-2 | Log of agent tool calls: query text, results returned, and failures (with personal data removed where it can be detected, D-24). | MVP |
| GOV-3 | A "gaps" report: questions that found no good match, so curators know what to define next. | P2 |
| GOV-4 | Traces compatible with OpenTelemetry. | P2 |

## 8. Evaluation

| ID | Requirement | Phase |
|---|---|---|
| EVL-1 | A golden set of at least 50 business questions on the sample domain, each with the expected metric, dimensions, filters, and result. Include ambiguous questions and questions with no valid answer. | MVP |
| EVL-2 | Retrieval scores: recall at 3 and mean reciprocal rank. | MVP |
| EVL-3 | Compile correctness: compiled SQL returns the expected result on the sample data. | MVP |
| EVL-4 | End-to-end agent comparison: an agent with the MCP server versus the same agent with raw schema access. | MVP |
| EVL-5 | Evals run with one command and in CI. | MVP |

## 9. Sample data

| ID | Requirement | Phase |
|---|---|---|
| DAT-1 | A synthetic consumer lending dataset: customers, branches, products, loan applications, contracts, installment schedules, payments, collection actions. Generated by a seeded script so it is reproducible. | MVP |
| DAT-2 | Realistic quirks: late payments, restructured contracts, multiple date columns with different meanings, a few personal-data fields. These make the semantic layer necessary. | MVP |
| DAT-3 | 15 to 25 approved metrics in the sample domain (for example disbursement amount, outstanding principal, days past due buckets, non-performing financing ratio, collection rate). | MVP |

## 10. Non-functional

| ID | Requirement | Phase |
|---|---|---|
| NFR-1 | One command starts the full development stack (Docker Compose): database with sample data, API, MCP server, web app (D-22). | MVP |
| NFR-2 | Search responds in under 500 ms at the 95th percentile on the sample domain. | MVP |
| NFR-3 | Can run fully self-hosted, with no required external service. | MVP |
| NFR-4 | Secrets come from the environment, never from files in the repository. | MVP |
| NFR-5 | Automated tests for every module, and a CI pipeline that runs tests, lint, type checks, and evals. | MVP |
