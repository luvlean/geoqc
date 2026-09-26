"""Shared fixtures. Owned by QA Agent after Phase 0; module agents add fixtures in tests/<module>/conftest.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

REPO_ROOT = Path(__file__).resolve().parents[1]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip db/network tests when the environment cannot run them."""
    from geoqc.db import db_available

    have_db = db_available(test=True)
    have_net = os.environ.get("GEOQC_NETWORK") == "1"
    skip_db = pytest.mark.skip(reason="TEST_DATABASE_URL not reachable")
    skip_net = pytest.mark.skip(reason="set GEOQC_NETWORK=1 to run network tests")
    for item in items:
        if "db" in item.keywords and not have_db:
            item.add_marker(skip_db)
        if "network" in item.keywords and not have_net:
            item.add_marker(skip_net)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def square() -> Polygon:
    """10x10 m square in a local metric frame."""
    return box(0, 0, 10, 10)


@pytest.fixture
def shifted_square() -> Polygon:
    """Same square shifted by 5 m along x: IoU with `square` is exactly 1/3."""
    return box(5, 0, 15, 10)


@pytest.fixture
def db_conn():
    """Connection to the test database with schema applied; tables truncated on exit."""
    from geoqc.db import apply_schema, connect

    with connect(test=True) as conn:
        apply_schema(conn)
        conn.commit()
        yield conn
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE discrepancies, matches, buildings_det, buildings_ref RESTART IDENTITY CASCADE"
            )
        conn.commit()
