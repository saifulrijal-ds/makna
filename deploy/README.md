# Development stack

`docker compose -f deploy/docker-compose.yml up -d --wait` starts everything (NFR-1). It
needs Docker with Compose v2 and one environment variable, with no default:

```
export MAKNA_SECRET_KEY=$(openssl rand -hex 32)
docker compose -f deploy/docker-compose.yml up -d --wait
curl http://127.0.0.1:8000/healthz        # {"status":"ok"}
uv run python -m deploy.check_stack       # the stack checks CI runs
docker compose -f deploy/docker-compose.yml down -v    # stop and delete the data
```

## Services

| Service | Image | What it does |
|---|---|---|
| `postgres` | `pgvector/pgvector:0.8.0-pg17` | PostgreSQL 17 with pgvector 0.8.0. Two databases, three roles (below). |
| `init` | built from `deploy/Dockerfile` | Runs once: `python -m deploy.init`. |
| `app` | built from `deploy/Dockerfile` | `makna serve` on port 8000 (`/healthz` for now). Starts after `init` succeeds. |

**Image pin.** PostgreSQL major version 17, pgvector 0.8.0, image tag
`pgvector/pgvector:0.8.0-pg17`. Change the tag in `docker-compose.yml`, in the CI `integration`
job, and here together. The Makna image installs `uv` 0.11.32 (the version CI uses) with pip.

**Ports.** Both published ports bind to `127.0.0.1` only: PostgreSQL on 5432
(`MAKNA_POSTGRES_PORT` changes it) and the app on 8000.

## Databases and roles

| Database | Owner | Roles that can connect |
|---|---|---|
| `makna` | role `makna` | `makna` (the application and its migrations) |
| `lending` | role `lending_loader` | `lending_loader` (creates and loads `lending.core`); `lending_reader` (`SELECT` only) |

The `makna` role cannot connect to `lending`, and `lending_reader` cannot connect to `makna`.
`deploy/postgres-init/01-roles.sh` creates them the first time the data volume is created.
The application reaches the sample data through `MAKNA_ENGINE_URL`, which the stack sets to
the `lending_reader` role.

Passwords come from the environment. Only these four have a development default in
`docker-compose.yml`, for the local database only: `MAKNA_POSTGRES_PASSWORD` (superuser),
`MAKNA_DB_PASSWORD`, `MAKNA_LENDING_LOADER_PASSWORD`, `MAKNA_LENDING_READER_PASSWORD`.
Passwords are placed in connection URLs, so use letters and digits or URL-encode them.
`deploy/.env.example` lists every variable with no value.

## The init runner

`deploy/init.py` runs an ordered list of named steps (`STEPS`) and prints one line per step.
It stops at the first failing step and exits 1. Every step is idempotent: a second run prints
`unchanged` for each and exits 0. Steps now:

1. `generate-sample-data`: writes the nine Parquet files to `data/generated` (a volume) if any is missing.
2. `load-lending`: creates `lending.core` tables from `data/generator/spec.py` and loads the files (`data/lending_loader.py`). A table is reloaded only when its file changed (its load marker is the file's SHA-256, kept as the table comment).
3. `migrate`: `alembic upgrade head` on the `makna` database.
4. `sync-catalog`: syncs `data/catalog/lending.catalog.yaml` into the catalog tables and runs the reference check against `semantic/definitions`. When the file has not changed it writes nothing (no `sync_run` row), so a second run prints `unchanged`.

5. `build-index`: indexes `semantic/definitions` and the synced catalog into `semantic_object` and `search_document`, with the provider from `config/makna.toml` / `MAKNA_EMBEDDING_PROVIDER` (default `fastembed`, whose model the image carries). It writes only differences, so a second run prints `unchanged` and writes nothing. If the model differs from the one that built the index, every document is embedded again.

Existing Parquet files are kept whatever `--seed` or `--scale` is passed (a file that cannot be read is regenerated); to change the data, delete the `generated` volume and the `pgdata` volume (`down -v`).

Later work packages append their step to `STEPS`. `init` lives here and not in `src/makna`
because it uses the sample-data code in `data/`, which the installed product never imports.

## The embedding model and the offline check

The image downloads the default model (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, about 0.22 GB, Apache-2.0) when it is built, into `/opt/makna/models`, and sets `HF_HUB_OFFLINE=1`; at run time the provider reads only those files and never tries the network (NFR-3). The model name and directory in the `Dockerfile` must equal `[embedding.fastembed]` in `config/makna.toml` (a test checks it). Outside Docker, `MAKNA_EMBEDDING_ALLOW_DOWNLOAD=true` lets the provider download the model into `cache_dir` once (or use `MAKNA_EMBEDDING_PROVIDER=hash`, which is for tests only). A site without internet access needs the model files in that directory; a build argument for an internal mirror is not built yet.

Two search checks run in the CI `stack` job (and can be run by hand):

```
docker compose -f deploy/docker-compose.yml exec -T app python -m deploy.check_search
```

builds the index with the real model, asserts that the model has 384 dimensions and is deterministic, that "pembiayaan bermasalah" returns `metric:npf_ratio` or `metric:npf_outstanding_principal` first, and that the 95th percentile of 200 searches is under 500 ms (NFR-2). It also prints the recall of the keyword leg, the vector leg, and the fused list (information only).

```
docker compose -p makna-offline -f deploy/docker-compose.yml -f deploy/docker-compose.offline.yml up -d --wait
docker compose -p makna-offline -f deploy/docker-compose.yml -f deploy/docker-compose.offline.yml exec -T app python -m deploy.check_search --require-offline
```

starts a second copy of the stack (its own project name, volumes, and a small data set) on a network with `internal: true`: containers reach each other and nothing else. `init` builds the index there with the model in the image, the check first proves that the container cannot connect to a public address, and CI also runs `makna index build` and `makna search` in that container. No port is published on that stack. Stop it with `down -v` and the same two `-f` options.

## Personal-data guardrails (governance mode)

`[governance] mode` in `config/makna.toml` (not an environment variable, and never a request
field) sets how strictly the compiler protects personal data for the whole deployment
(`docs/ARCHITECTURE.md` section 7.6, D-29):

- `strict` (the default): masks, no filters on personal dimensions, no unmasked personal
  dimensions, and groups smaller than `min_group_size` (default 10) left out.
- `custom`: relax only the rules you set (`min_group_size = 0` or at least 2,
  `allow_personal_filters`, `apply_masks`, `allow_unmasked_personal`); a rule you leave out keeps
  its strict value.
- `off`: every rule relaxed.

Setting `allow_personal_filters`, `apply_masks`, or `allow_unmasked_personal` while the mode is
not `custom` is an error, and so is a `min_group_size` of 1, a negative one, or 0 outside
`custom`. When the policy relaxes at least one rule, every compile response carries a warning that
names the mode and each relaxed rule. With `allow_personal_filters`, the minimum group size does
not apply to filters: a filter on an identifier can select one person's records. With
`apply_masks = false` a masked dimension returns the raw value of its column (`customer.birth_year`
returns full birth dates), which can be much finer than its name. `makna validate` always checks in `strict` mode. Relaxing the rules does not
replace access control in the engine: restrict or mask personal data there too.

## Migrations

`uv run alembic upgrade head` and `uv run alembic downgrade base` work from the repository
root with `MAKNA_DATABASE_URL` set (`alembic.ini` points into `src/makna/index/migrations`).
`downgrade base` removes the tables and leaves the `vector` extension.

## Tests

The integration tier needs PostgreSQL with pgvector. Point `MAKNA_TEST_DATABASE_URL` at a
server where the role may create databases and the `vector` extension, for example the stack:

```
MAKNA_TEST_DATABASE_URL=postgresql://postgres:makna_dev_postgres@127.0.0.1:5432/postgres \
  uv run pytest -m integration
```

The engine tier reads the sample data as the read-only `lending_reader` role through
`MAKNA_ENGINE_URL` (the same variable the app uses). Against the running stack:

```
MAKNA_ENGINE_URL=postgresql://lending_reader:makna_dev_reader@127.0.0.1:5432/lending \
  uv run pytest -m engine
```

The password is the development default of `MAKNA_LENDING_READER_PASSWORD`; change the URL if
you changed the variable or the published port. Without `MAKNA_ENGINE_URL` the engine tests
skip; the CI `stack` job also sets `MAKNA_REQUIRE_ENGINE_DATABASE=1`, which turns a missing URL
into a failure. The tier now holds the golden reference SQL check (`evals/golden/`, WP-14):
every file in `evals/golden/reference_sql/` runs on the sample data and must return the
committed CSV in `evals/golden/expected/`. After a deliberate change to the generator or a
definition, rewrite the CSVs with `uv run python -m evals.golden.freeze` (it needs the same
variable) and review the diff.

### The engine connector and the compiled SQL (WP-11)

The engine tier also holds the connector tests (`tests/engine/test_postgres_connector.py`:
tables of `core`, the newest `snapshot_date`, the statement timeout, a read-only connection)
and the compiled-SQL tests (`tests/engine/test_compiled_sql.py`). The latter compile with the
product compiler for `postgres`, run the SQL on the sample data, and compare it with
hand-written reference queries (the five cases of `spikes/compiler/run_on_postgres.py`) and with
the columns of `docs/ARCHITECTURE.md` section 7.2 (every approved sample metric, at its default
grain). The CI `stack` job also runs `makna catalog compare` in the `app` container, which must
print "no differences". Locally, a database that is not called `lending` cannot resolve the
three-part names the compiler writes (`lending.core.<table>`); the test helper then removes the
`lending` qualifier from the tables and changes nothing else. In the stack and in CI the SQL
runs exactly as printed.

**Open points of `spikes/compiler/README.md` ("What is not proven"), and what the engine tier
shows.** The proof is the CI `stack` job on PostgreSQL 17; a run on a local PostgreSQL 16.2
holding the same data (seed 20261111, scale 1.0) gave the same result and is supporting
evidence only.

| Open point | Result |
|---|---|
| SQL not executed on Trino; the first package with a target engine runs the five cases. | Trino is put aside (D-22). The five cases, compiled by the product compiler for `postgres`, run on the sample database and equal their reference queries (`test_a_spike_case_compiled_by_the_product_equals_its_reference_query`). Nothing had to change in the compiler. |
| sqlglot prints the unit as `'MONTH'` in upper case; confirm it is accepted. | Confirmed: `DATE_TRUNC('MONTH', ...)` and `DATE_TRUNC('QUARTER', ...)` run on PostgreSQL (cases 1, 2 and 4). |
| `SUBSTRING`-based partial masking ran on DuckDB only. | No longer applies: partial masking was removed from the design (review R-4). |
| The minimum group size rule is not in the spike. | Not exercised here: none of the five cases or the no-dimension metric runs selects a personal dimension. It is covered by the compiler unit tests on DuckDB, and its PostgreSQL behaviour was checked by hand in WP-10 (`docs/notes/postgres-rejects-a-bare-string-constant-in-group-by.md`). |
| Performance on large tables was not measured. | Still not measured. On the scale 1.0 sample (about 81,000 snapshot rows) each compiled case ran in well under a second locally. |

Any PostgreSQL holding the generated data in schema `core` will do, for example one loaded
with `data.lending_loader.load_all` (the reference SQL names tables as `core.<table>`, which
resolves in a database of any name).
