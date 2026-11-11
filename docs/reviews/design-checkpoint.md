# Design Checkpoint

As of 2026-11-11, branch `design/architecture`. Read this first when picking up the work after a break or a context reset. It points to the sources of truth and does not replace them.

## Where things stand

- **The design is reviewed and fixed. No build work package has started**: all 28 packages in `docs/progress.json` are `not_started`.
- **The independent review** is in `docs/reviews/design.md`. Of its 20 findings, the 12 High and Medium ones (R-1 to R-12) are fixed. Section 4 of that file says how each was fixed, and section 5 says how the owner decisions changed them.
- **Owner decisions D-1 to D-26** are in `docs/DECISIONS.md`. These shape the build most:
  - D-21: Makna compiles only and never runs a caller's query.
  - D-22: the MVP target engine is PostgreSQL, with DuckDB in unit tests; Trino is put aside.
  - D-17: named rules.
  - D-18: example requests, which must compile (V19).
  - D-19: English and Indonesian synonyms are required.
  - D-20: the governance team approves any change that loosens sensitivity.
  - D-13: the agent eval runs on Gemini with a key from `GEMINI_API_KEY`, which the owner provides at WP-22B.
  - D-14: part of the catalog metadata is deliberately poor.
  - D-15: coding sessions may have no Docker.
- **All 38 MVP requirement IDs** have a work package (QRY-4 is dropped). `docs/PLAN.md`, `docs/progress.json` and `docs/ARCHITECTURE.md` section 16 agree.

## Evidence that holds at this commit

| Check | Command (from the repository root) | Result |
|---|---|---|
| Example YAML against the schema | `uv run --no-project --with jsonschema --with pyyaml python docs/reviews/design-probes/validate_examples.py` | 16 of 16 pass |
| Compiler spike | `uv run --no-project --with sqlglot --with duckdb --with pyyaml python spikes/compiler/run_spike.py` | All checks pass, including the 7 example requests |
| Compiler cases on PostgreSQL and DuckDB | `uv run --no-project -p 3.12 --with sqlglot --with duckdb --with pyyaml --with pgserver --with "psycopg[binary]" python spikes/compiler/run_on_postgres.py` | 10 of 10 match the references |
| Search spike | `uv run --no-project -p 3.12 --with pgserver --with "psycopg[binary]" --with pyyaml --with numpy python spikes/search/run_spike.py` | Matches `output.txt` except the latency line |

## Open items (none blocks WP-01)

- **Low review findings R-13 to R-20** are listed in `docs/reviews/design.md` section 4, each with the package that should fix it. Two matter early:
  - WP-14 needs the `freeze` command, which is planned in WP-15 (R-13).
  - WP-18 acceptance 2 uses the MCP tools but does not depend on WP-13 (R-13).
- **The residual risk of D-21:** an agent with its own engine access can run SQL that Makna never compiled. Adopters must protect personal data in the engine as well (`docs/notes/makna-guarantees-cover-only-the-sql-it-compiles.md`).
- **Not yet measured:** the multilingual embedding model's quality and latency (WP-07, WP-15), and the full EVL-4 agent run (WP-22B, needs the owner's key).

## Next step

The owner merges the design pull request. Building then starts with WP-01, which unlocks WP-02 and WP-03. Follow `docs/PLAN.md` ("How to use this plan"): one package per branch and pull request, and update `docs/progress.json` after each step.
