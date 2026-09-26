#!/usr/bin/env bash
# Wait until PostgreSQL accepts connections.
#   scripts/ci/wait_for_db.sh [DSN]      DSN defaults to $DATABASE_URL, then the .env.example value
# Env: DB_WAIT_TIMEOUT (seconds, default 60), PY (python with psycopg; default .venv/bin/python or python)
set -euo pipefail
cd "$(dirname "$0")/../.."

DSN=${1:-${DATABASE_URL:-postgresql://geoqc:geoqc@localhost:5433/geoqc}}
TIMEOUT=${DB_WAIT_TIMEOUT:-60}
if [ -z "${PY:-}" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python; fi
fi
SAFE_DSN=$(printf '%s' "$DSN" | sed -E 's#(://[^:/@]+:)[^@]*@#\1***@#')

for _ in $(seq 1 "$TIMEOUT"); do
  if "$PY" - "$DSN" <<'PYEOF' 2>/dev/null
import sys
import psycopg
psycopg.connect(sys.argv[1], connect_timeout=2).close()
PYEOF
  then
    echo "database is ready: $SAFE_DSN"
    exit 0
  fi
  sleep 1
done

echo "error: database not reachable after ${TIMEOUT}s: $SAFE_DSN" >&2
exit 1
