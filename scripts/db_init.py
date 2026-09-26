"""Apply ``sql/*.sql`` to a database through ``geoqc.db.apply_schema``.

Fallback used by ``make db-init`` until ``geoqc matching init-db`` (docs/CONTRACTS.md §6)
is delivered; also handy for the test database (``--test``). Idempotent by contract.

Usage::

    .venv/bin/python scripts/db_init.py            # DATABASE_URL from .env
    .venv/bin/python scripts/db_init.py --test     # TEST_DATABASE_URL from .env
    .venv/bin/python scripts/db_init.py --dsn postgresql://user:pass@host:port/db
"""

from __future__ import annotations

import argparse
import sys

from geoqc.db import SQL_DIR, apply_schema, connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--test", action="store_true", help="use TEST_DATABASE_URL instead of DATABASE_URL"
    )
    parser.add_argument("--dsn", default=None, help="explicit connection string (overrides .env)")
    args = parser.parse_args(argv)

    with connect(args.dsn, test=args.test) as conn:
        applied = apply_schema(conn)

    if not applied:
        print(f"warning: no sql/NNN_*.sql files found in {SQL_DIR}", file=sys.stderr)
        return 1
    target = "TEST_DATABASE_URL" if args.test else ("--dsn" if args.dsn else "DATABASE_URL")
    for name in applied:
        print(f"applied {name}")
    print(f"schema applied to {target}: {len(applied)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
