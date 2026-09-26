"""Fixtures for API tests: isolated app on the test DSN and a synthetic review queue."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from geoqc.api.main import create_app
from geoqc.db import get_dsn

# Synthetic AOI (~3 x 3 km near Saint Petersburg) used by every DB test.
AOI_BBOX = (30.30, 59.93, 30.36, 59.96)
AOI_BBOX_STR = ",".join(str(v) for v in AOI_BBOX)


def square_wkt(lon: float, lat: float, half: float = 0.00025) -> str:
    """Axis-aligned square (WKT POLYGON) centred at lon/lat, ``half`` degrees half-width."""
    return (
        f"POLYGON(({lon - half} {lat - half}, {lon + half} {lat - half}, "
        f"{lon + half} {lat + half}, {lon - half} {lat + half}, {lon - half} {lat - half}))"
    )


@dataclass
class Seed:
    """Ids of the synthetic rows: d1 > d4 > d2 > d3 by confidence, d4 lies outside the AOI."""

    ref_a: int
    ref_b: int
    det_a: int
    det_b: int
    d1: int  # missing_in_map, 0.90, new, det_a, inside AOI
    d2: int  # missing_on_image, 0.60, new, ref_a (osm 111), inside AOI
    d3: int  # geometry_mismatch, 0.30, confirmed, ref_b + det_b, iou 0.3, inside AOI
    d4: int  # missing_in_map, 0.75, rejected, far outside AOI


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """App bound to the TEST database, no Esri key, static files disabled."""
    app = create_app(dsn=get_dsn(test=True), arcgis_api_key="", serve_static=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seed(db_conn) -> Seed:
    """Insert reference/detected buildings and four discrepancies; truncated by ``db_conn``."""

    def insert(sql: str, params: list) -> int:
        return int(db_conn.execute(sql, params).fetchone()["id"])

    geom = "ST_Multi(ST_GeomFromText(%s, 4326))"
    ref_a = insert(
        f"INSERT INTO buildings_ref (osm_id, geom, tags, source) VALUES (111, {geom}, '{{}}', 'osm') RETURNING id",
        [square_wkt(30.33, 59.95)],
    )
    ref_b = insert(
        f"INSERT INTO buildings_ref (osm_id, geom, tags, source) VALUES (222, {geom}, '{{}}', 'osm') RETURNING id",
        [square_wkt(30.35, 59.935)],
    )
    det_a = insert(
        f"INSERT INTO buildings_det (geom, score, model_version, tile_id) VALUES ({geom}, 0.95, 'test-00000000', '0_0') RETURNING id",
        [square_wkt(30.31, 59.94)],
    )
    det_b = insert(
        f"INSERT INTO buildings_det (geom, score, model_version, tile_id) VALUES ({geom}, 0.80, 'test-00000000', '0_1') RETURNING id",
        [square_wkt(30.3503, 59.935)],
    )
    disc_sql = (
        "INSERT INTO discrepancies (class, ref_id, det_id, iou, confidence, geom, status, model_version)"
        f" VALUES (%s, %s, %s, %s, %s, {geom}, %s, 'test-00000000') RETURNING id"
    )
    d1 = insert(
        disc_sql, ["missing_in_map", None, det_a, None, 0.90, square_wkt(30.31, 59.94), "new"]
    )
    d2 = insert(
        disc_sql, ["missing_on_image", ref_a, None, None, 0.60, square_wkt(30.33, 59.95), "new"]
    )
    d3 = insert(
        disc_sql,
        ["geometry_mismatch", ref_b, det_b, 0.30, 0.30, square_wkt(30.35, 59.935), "confirmed"],
    )
    d4 = insert(
        disc_sql, ["missing_in_map", None, None, None, 0.75, square_wkt(31.0, 60.5), "rejected"]
    )
    db_conn.commit()
    return Seed(ref_a, ref_b, det_a, det_b, d1, d2, d3, d4)
