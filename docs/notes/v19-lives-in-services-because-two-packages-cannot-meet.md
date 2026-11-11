The V19 check (example requests plan) is in `makna.services.examples`, because it needs `makna.definitions` (the loader) and `makna.compiler` (the planner), and neither may import the other.

- `makna.definitions.validate` takes `extra_checks`, so `makna validate` (cli, which may import services) passes `check_example_requests` and the definitions package stays model-only.
- The check plans with `enforce_status=False`: it runs at submit and approve, before a metric is approved, so the status gate must not fire.
- A metric whose references do not resolve gets no V19 error; V07 to V09 and V18 already report it.
- Entities and metrics are resolved like `validate` does: official file first, then proposal, and the metric under check is always itself.
- Since WP-09 the check plans, generates SQL for `postgres`, and re-parses it with sqlglot's `postgres` dialect. It runs no SQL, so `makna validate` needs no database.
