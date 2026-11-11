# Decisions and Open Questions

Status: DRAFT for review (2026-11-11).

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
| D-7 | Trino is the reference query engine. | 2026-11-11 |
| D-8 | Python is the primary language across the whole platform: backend, connectors, query compiler, MCP server, evals, data generation, and the web app's server side. JavaScript or TypeScript is allowed only for browser-side pieces that Python cannot provide (for example a code editor widget or a diagram), kept small and without a separate frontend build where possible. Reason: the owner and the teams most likely to adopt and maintain the platform work in Python. | 2026-11-11 |
| D-9 | The project is named Makna. The repository is `makna`, the Python distribution name is `makna-sl`, and the import package is `makna`. Publishing to PyPI is not decided yet, so do not set up publishing. | 2026-11-11 |

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

## Open questions for the design session

Answer each in `docs/ARCHITECTURE.md` or a new decision record, with the reasoning.

| ID | Question |
|---|---|
| Q-1 | Build our own semantic model schema, or adopt or extend an existing format (MetricFlow, OSI, Malloy)? What do we lose in each case? |
| Q-2 | Which open-source catalog is the first connector target, and what is the minimal common metadata model across catalogs? |
| Q-3 | How is a natural-language question resolved to a metric request? Where does deterministic logic end and an LLM begin? How are ambiguous questions handled (ask back, return options, or pick a default)? |
| Q-4 | How do YAML files in git and edits in the web app stay consistent? Does the app commit to git, open pull requests, or own a store that exports to git? |
| Q-5 | How are sensitivity tags and masking rules defined and enforced in compiled SQL? |
| Q-6 | What is the minimal role and permission model for the MVP? |
| Q-7 | What exactly do the MCP tools return, so agents use few tokens and still have enough to answer? |
| Q-8 | What is the shape of the golden question set, and how is an end-to-end agent run scored? |
