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
