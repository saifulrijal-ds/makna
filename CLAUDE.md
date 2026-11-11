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
- Integration tests (need PostgreSQL with pgvector in `MAKNA_TEST_DATABASE_URL`): `uv run pytest -m integration`
- Engine tests (need the generated sample data in PostgreSQL): `uv run pytest -m engine`
- Lint: `uv run ruff check .`; format check: `uv run ruff format --check .` (apply with `uv run ruff format .`)
- Types: `uv run mypy` (strict for `makna.model` and `makna.compiler`)
- CLI: `uv run makna --help`
- Sample data: `uv run python -m data.generator --out data/generated --seed 20261111 --scale 1.0` (git-ignored output; `--scale 0.05` for a quick set)
- Settings: copy the names in `deploy/.env.example` into your environment; non-secret settings are in `config/makna.toml`.

`spikes/` and `docs/` are not linted or type-checked. To be added by later work packages: start the stack (WP-05) and run evals (WP-15).
