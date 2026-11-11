#!/bin/bash
# Runs once, when the PostgreSQL data directory is first created (docker-entrypoint-initdb.d).
# Creates the two databases and their separate roles (ARCHITECTURE section 9):
#   makna    database, owned by role makna (the application and its migrations)
#   lending  database, owned by role lending_loader (the sample data); read by lending_reader
# The makna role cannot connect to lending, and lending_reader can only read it.
set -euo pipefail

: "${MAKNA_DB_PASSWORD:?}" "${MAKNA_LENDING_LOADER_PASSWORD:?}" "${MAKNA_LENDING_READER_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v makna_pw="$MAKNA_DB_PASSWORD" \
  -v loader_pw="$MAKNA_LENDING_LOADER_PASSWORD" \
  -v reader_pw="$MAKNA_LENDING_READER_PASSWORD" <<'SQL'
CREATE ROLE makna LOGIN PASSWORD :'makna_pw';
CREATE ROLE lending_loader LOGIN PASSWORD :'loader_pw';
CREATE ROLE lending_reader LOGIN PASSWORD :'reader_pw';
-- Whatever the reader's session asks for, it starts read-only; privileges below are the real limit.
ALTER ROLE lending_reader SET default_transaction_read_only = on;

CREATE DATABASE makna OWNER makna;
CREATE DATABASE lending OWNER lending_loader;

-- Nobody connects unless granted: owners keep CONNECT, the reader gets lending only.
REVOKE ALL ON DATABASE makna FROM PUBLIC;
REVOKE ALL ON DATABASE lending FROM PUBLIC;
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
REVOKE ALL ON DATABASE template1 FROM PUBLIC;
GRANT CONNECT ON DATABASE lending TO lending_reader;

\connect makna
CREATE EXTENSION IF NOT EXISTS vector;

\connect lending
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA core AUTHORIZATION lending_loader;
GRANT USAGE ON SCHEMA core TO lending_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lending_loader IN SCHEMA core
  GRANT SELECT ON TABLES TO lending_reader;
SQL
