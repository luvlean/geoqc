"""Thin PostGIS access layer shared by data, matching, eval and api modules.

Uses psycopg 3. Callers must not build SQL by string formatting with user input.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from geoqc.config import REPO_ROOT, get_settings

SQL_DIR = REPO_ROOT / "sql"


def get_dsn(test: bool = False) -> str:
    s = get_settings()
    return s.test_database_url if test else s.database_url


@contextmanager
def connect(
    dsn: str | None = None, *, test: bool = False, autocommit: bool = False
) -> Iterator[psycopg.Connection]:
    """Yield a connection with dict rows; commits on success, rolls back on error."""
    conn = psycopg.connect(dsn or get_dsn(test=test), row_factory=dict_row, autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    """Execute a whole .sql file (DDL / functions). Statements are separated by ';' at line end."""
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)


def apply_schema(conn: psycopg.Connection, sql_dir: Path = SQL_DIR) -> list[str]:
    """Apply every ``sql/NNN_*.sql`` in lexical order. Idempotent by contract."""
    applied: list[str] = []
    for path in sorted(sql_dir.glob("[0-9][0-9][0-9]_*.sql")):
        run_sql_file(conn, path)
        applied.append(path.name)
    return applied


def db_available(dsn: str | None = None, *, test: bool = False) -> bool:
    """True when a connection can be opened (used by tests to skip gracefully)."""
    try:
        with psycopg.connect(dsn or get_dsn(test=test), connect_timeout=2):
            return True
    except Exception:
        return False
