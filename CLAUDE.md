# Working Rules for Agents in This Repository

This repository builds Makna, an open semantic layer platform: a web app, connectors to data catalogs and query engines, hybrid search, a query compiler, and an MCP server for agents. Read `docs/PROJECT_BRIEF.md` first. Read `docs/DECISIONS.md` before you make a design choice.

## Sources of truth

- What we build and why: `docs/PROJECT_BRIEF.md`
- Requirements and their IDs: `docs/REQUIREMENTS.md`
- Decisions and open questions: `docs/DECISIONS.md`
- Architecture (written in the design session): `docs/ARCHITECTURE.md`
- Work plan and status: `docs/PLAN.md` and `docs/progress.json`
- Lessons learned: `docs/notes/` (one lesson per file, one-line summary at the top)

If two of these disagree, say so in your report. Do not silently pick one.

## Language

Python is the primary language for everything, including the web app's server side (decision D-8). Use JavaScript or TypeScript only for small browser-side pieces that Python cannot provide, and say why in the pull request. Do not add a separate frontend build without a decision record.

## Data rules

- Use only the synthetic data in `data/`. Never add real company data, real schemas, real credentials, or real personal data to this repository, even as an example.
- Secrets come from environment variables. Never commit a `.env` file with values.

## How to work

- Read the code before you change or describe it. Do not make claims about code you have not opened.
- Keep changes to what the task needs. Do not add features, abstractions, or configuration for hypothetical future needs. Validate input only at system boundaries.
- Write a general solution, not one that only passes the tests. If a test is wrong, say so instead of working around it. Do not delete or weaken tests to make them pass.
- Reference requirement IDs (for example `SRC-1`) in commit messages and pull request descriptions.
- Testing without Docker (decision D-15). Your session may have no Docker, so no dev stack. The target engine is PostgreSQL (decision D-22). Run the unit tier locally; compiler tests run the generated SQL on DuckDB. Run the integration and engine tiers only if a PostgreSQL (with pgvector for integration) is available. Treat PostgreSQL and dev-stack results as proven only when CI on GitHub Actions passes; a DuckDB pass is not proof that the SQL works on PostgreSQL. Details in `docs/PLAN.md` ("How to use this plan").
- For structure questions (what calls what, what a change affects, where a name is defined) you may ask the code graph first, if `graphify-out/graph.json` exists (command under Commands). It is an aid, not a source of truth: it misses wiring made at run time, such as MCP tool tables and pytest fixtures (`docs/notes/the-code-graph-misses-runtime-wiring.md`), and it is stale after changes. Read the lines it points to, and confirm with search anything it says is missing.
- After a meaningful step, update `docs/progress.json` and, if you learned something reusable, add or update a note in `docs/notes/`.

## Git

- Work on a branch named `<area>/<short-description>`. Never commit directly to `main`.
- Commit small, working steps with clear messages.
- Open a pull request when a work package is complete and its checks pass. The project owner merges.

## Actions that need the project owner

Ask before: force-pushing, deleting branches you did not create, changing CI secrets or repository settings, or adding a dependency with a non-permissive license.

These do not need approval: creating branches, committing, pushing your own branch, opening pull requests, installing dependencies with permissive licenses, running tests and evals, and retrying commands that failed for ordinary reasons.

## Commands

Setup and checks (uv, Python 3.12 or newer):

- Install: `uv sync`
- Unit tests (the tier to run locally): `uv run pytest -m unit`
- Integration tests (need PostgreSQL with pgvector in `MAKNA_TEST_DATABASE_URL`, a role that may create databases and the `vector` extension; each test uses its own scratch database): `uv run pytest -m integration`
- Engine tests (need the generated sample data in PostgreSQL, read through `MAKNA_ENGINE_URL` as the `lending_reader` role; without the variable they skip, and with `MAKNA_REQUIRE_ENGINE_DATABASE` set a missing URL fails): `uv run pytest -m engine` (the compile-eval tests there also need `MAKNA_TEST_DATABASE_URL`, as the integration tier, and skip without it). Against the stack: `MAKNA_ENGINE_URL=postgresql://lending_reader:makna_dev_reader@127.0.0.1:5432/lending uv run pytest -m engine`. Rewrite the golden expected CSVs only after a deliberate data or definition change: `uv run python -m evals freeze`
- Evals (retrieval, compile, and the resolver score, docs/EVALS.md section 6; `makna-eval` is run as `python -m evals`, because the runner lives outside the installed package). They need the application database with the catalog synced and the index built with the real embedding model (`MAKNA_DATABASE_URL`, `MAKNA_EMBEDDING_CACHE_DIR`) and the sample data (`MAKNA_ENGINE_URL`, read-only role): `uv run python -m evals all` (or `retrieval`, `compile`). The report goes to `evals/reports/<date>/` (git-ignored); the exit code is 1 when a gate of `evals/gates.toml` fails. `all` also reports the rule-based resolver's exact-request accuracy on the golden `answer` questions (today = 2026-07-01; informative, no gate). `--verbose` prints engine error text and differing values to standard error; they can hold personal data, so never into a shared log.
- Governance golden file (a change detector for compiler output, D-32 (1)): `uv run python -m tests.unit.golden.make_governance_golden`. A package that changes compiler output regenerates it in the same pull request with that pull request's compiler, lists the changed cases by category with the reason, and the task reviewer checks every category; the owner's merge approves the diff. Otherwise leave it alone. Details in the script's docstring.
- Start the stack (needs Docker; `MAKNA_SECRET_KEY` has no default and must be at least 32 characters): `export MAKNA_SECRET_KEY=$(openssl rand -hex 32); docker compose -f deploy/docker-compose.yml up -d --wait`; check it with `uv run python -m deploy.check_stack`; stop with `docker compose -f deploy/docker-compose.yml down -v`. Details, roles and ports in `deploy/README.md`. For the integration tier against the stack: `MAKNA_TEST_DATABASE_URL=postgresql://postgres:makna_dev_postgres@127.0.0.1:5432/postgres`.
- Sync from OpenMetadata (CON-2, WP-21): `uv run makna catalog sync --source openmetadata` (default `--source file`; `--file` is for the file source only). It reads `[catalog.openmetadata]` of `config/makna.toml` (URL, service-to-engine-catalog map, page size, timeout) and the token from `MAKNA_OPENMETADATA_TOKEN` only; without the token it exits 2. The optional server stack is `docker compose -f deploy/docker-compose.openmetadata.yml up -d --wait` (OpenMetadata 2.0.7; needs Docker), loaded with `uv run python -m deploy.seed_openmetadata`. Neither has run against a real server yet (manual criterion 4). Never add `openmetadata-ingestion` (a test checks `uv.lock`).
- Data freshness (needs `MAKNA_DATABASE_URL` and `MAKNA_ENGINE_URL`, the read-only account; exit 0 every measure checked, 1 a measure failed or the application database cannot be used, 2 a setting is missing): `uv run makna freshness refresh` asks the target database for the newest value of the time dimension of each approved measure and stores it in `data_freshness`; the metric pages show it as "data up to 2026-06-30". A failed measure is reported by its id and the error class only, never the engine's message. The development stack runs it in `init` (step `refresh-freshness`).
- Compare the catalog copy with the target database (needs `MAKNA_DATABASE_URL` and `MAKNA_ENGINE_URL`; exit 0 no differences, 1 differences, 2 setting or connection problem): `uv run makna catalog compare`
- Users and API tokens (need `MAKNA_DATABASE_URL`; they act as the operator; the password is asked for or read with `--password-stdin`, never an argument; a token is shown once): `uv run makna user create --email E --name N --role R [--team T]`, `uv run makna user list`, `uv run makna user deactivate --email E`, `uv run makna token create --user E --name N`, `uv run makna token list`, `uv run makna token revoke --id N`. Users and tokens are never deleted. The development stack creates demo users only when `MAKNA_DEMO_PASSWORD` is set.
- MCP: `makna serve` serves MCP over Streamable HTTP at `/mcp` (`Authorization: Bearer <token>`, a token from `makna token create`; a `Host` other than localhost must be listed in `[mcp] allowed_hosts` of `config/makna.toml`). For clients that can only start a process, the stdio bridge needs no database setting: `MAKNA_URL=http://localhost:8000 MAKNA_TOKEN=<token> uv run makna mcp` (`MAKNA_URL` is the base URL, `/mcp` is appended, `http://` only for localhost; the token is never an option).
- Web app (WP-16): `makna serve` serves the pages at `/` next to `/mcp`. On the development stack open `http://localhost:8000/login` and sign in with a demo user of `deploy/README.md` (for example `admin@demo.example` and the `MAKNA_DEMO_PASSWORD` you started the stack with); the stack sets `MAKNA_WEB_DEVELOPMENT=true`, so the session cookie is not `Secure` over plain http. Outside the stack the cookie is `Secure`, so serve the pages over https and leave `MAKNA_WEB_DEVELOPMENT` unset. The discovery pages (WP-17) are `/` (search box, domains, recently approved metrics), `/search`, `/domains/<name>`, `/metrics/<name>`, `/entities/<name>` and `/glossary/<name>`; their tests are `tests/integration/test_web_discovery.py` and `tests/integration/test_freshness.py`. Users and API tokens can also be managed at `/admin/users` and `/admin/tokens` (admin) and `/account` (own tokens). The web tests are `tests/unit/test_web_security.py` and `tests/integration/test_web_*.py` (`uv run pytest -m integration -k web`). The vendored scripts and styles under `src/makna/web/static/vendor/` are listed with hashes in `VENDORED.md`; there is no Node.js or build step.
- Definitions repository (WP-18): with `MAKNA_DEFINITIONS_REPO` set to a git repository of its own the application writes definitions by committing (a path inside another repository stays a read-only plain directory). `init` seeds an empty repository from `semantic/definitions` once. `uv run makna index sync` (also run at application start) completes interrupted actions, records outside commits as external changes (indexed as proposals in review, never approved) and rebuilds the index; running it twice changes nothing. A configured repository that is broken (a `.git` that is a link or a file, a bare repository, another owner) or whose history was rewritten is an error, never read as plain files; after a rewrite, `makna index sync --accept-rewritten-history` is the operator's decision. The Docker image carries git.
- Test bench (WP-20): `/bench?q=<question>` (every signed-in role) shows the search hits with scores, the rule-based resolution, the request as editable JSON (a POST recompiles it), and the compiled SQL with a copy button. It compiles only and never runs SQL (D-21). `Services.resolve(caller, question, today)` is the service behind it (the `makna.resolve` rules need no language model; `today` is always a parameter). The one script it needs, `src/makna/web/static/bench.js`, copies the SQL to the clipboard, which only script can do. Tests: `tests/unit/test_resolve_grammar.py`, `tests/unit/test_resolver.py`, `tests/integration/test_service_resolve.py`, `tests/integration/test_web_bench.py`.
- Review and audit (WP-19B): `/review` lists every proposal in review, outside changes included, with the summary of the difference, the marks (D-17 users of a rule, D-20 governance team) and who may decide; `/review/<kind>/<name>` shows the full difference and the Approve and Reject form (curator and above; the buttons are a hint and the lifecycle decides again); `/audit` lists `audit_event` newest first, filtered by `object` and `action`, paged by `before=<id>`. Tests: `tests/unit/test_reviewing.py`, `tests/integration/test_web_review.py` (`uv run pytest -m integration -k review`).
- Editor (WP-19): `/new` and `/new/<kind>` and `/edit/<kind>/<name>` (curator and above) hold the YAML in Ace (`static/editor.js`; without script it is a text area), with Check (V01 to V23, line numbers, writes nothing), Save draft, Submit for review, and a difference view; a save is a `Lifecycle` call, so the commit and `audit_event` row are WP-18's. Tests: `tests/integration/test_editor_service.py`, `tests/integration/test_web_editor.py` (`uv run pytest -m integration -k editor`). After changing Ace or its styles, load the page in a browser and look for policy violations (`docs/notes/ace-needs-use-strict-csp-and-its-own-style-files.md`).
- Migrations (from the repository root, with `MAKNA_DATABASE_URL` set): `uv run alembic upgrade head`, `uv run alembic downgrade base`.
- Lint: `uv run ruff check .`; format check: `uv run ruff format --check .` (apply with `uv run ruff format .`)
- Types: `uv run mypy` (strict for `makna.model` and `makna.compiler`)
- CLI: `uv run makna --help`
- Sample data: `uv run python -m data.generator --out data/generated --seed 20261111 --scale 1.0` (git-ignored output; `--scale 0.05` for a quick set)
- Settings: copy the names in `deploy/.env.example` into your environment; non-secret settings are in `config/makna.toml`.
- Code graph (optional, D-36): `uv run --group graph graphify update .` builds a graph of the code in `graphify-out/` (git-ignored, about 12 seconds, local parsing only, no model calls). Then `uv run --group graph graphify query "<question>"`, `... path "<A>" "<B>"`, `... explain "<name>"` and `... affected "<name>"`. Rebuild it after a large change. Never run the passes that call a model (`label`, `extract`, `/graphify` on documents).

`spikes/` and `docs/` are not linted or type-checked. To be added by later work packages: the agent eval, `python -m evals agent` (WP-22B).
