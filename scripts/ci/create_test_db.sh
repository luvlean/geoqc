#!/usr/bin/env bash
# Create the test database (default geoqc_test) with the PostGIS extension, idempotently.
# Connects with the admin DSN ($DATABASE_URL) and derives the test DSN by swapping the dbname.
# Env: DATABASE_URL, TEST_DB_NAME (default geoqc_test), PY (python with psycopg)
set -euo pipefail
cd "$(dirname "$0")/../.."

ADMIN_DSN=${DATABASE_URL:-postgresql://geoqc:geoqc@localhost:5433/geoqc}
TEST_DB=${TEST_DB_NAME:-geoqc_test}
if [ -z "${PY:-}" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python; fi
fi

"$PY" - "$ADMIN_DSN" "$TEST_DB" <<'PYEOF'
import sys

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

admin_dsn, name = sys.argv[1], sys.argv[2]

with psycopg.connect(admin_dsn, autocommit=True) as conn:
    exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if exists:
        print(f"database {name} already exists")
    else:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        print(f"created database {name}")

info = conninfo_to_dict(admin_dsn)
info["dbname"] = name
with psycopg.connect(make_conninfo(**info), autocommit=True) as conn:
    conn.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    version = conn.execute("SELECT postgis_lib_version()").fetchone()[0]
    print(f"postgis {version} enabled in {name}")
PYEOF
