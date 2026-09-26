"""Fixtures for matching tests: synthetic geometries in metres (UTM 37N) transformed to EPSG:4326."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

METRIC_SRID = 32637  # fixed UTM zone for tests; real AOI zone comes from config/pipeline.yaml
X0, Y0 = 500_000.0, 6_180_000.0  # local origin inside the zone (~55.7N, 39E)
_TO_WGS = Transformer.from_crs(METRIC_SRID, 4326, always_xy=True)

MATCHING_CFG: dict[str, Any] = {
    "iou_match": 0.5,
    "iou_mismatch_min": 0.1,
    "dedup_iou": 0.7,
    "algorithm": "greedy",
    "dedup_detections": True,
    "split_multipart_detections": True,
    "aoi_path": None,
    "aoi_edge_buffer_m": 10,
    "aoi_edge_factor": 0.5,
    "min_area_m2": 20,
    "max_area_m2": 5000,
    "weights": {
        "missing_in_map": {"score": 0.5, "area": 0.2, "secondary": 0.3},
        "missing_on_image": {"area": 0.3, "secondary": 0.4, "context": 0.3},
        "geometry_mismatch": {"score": 0.4, "iou": 0.4, "area": 0.2},
    },
}


def sq(x0: float, y0: float, x1: float, y1: float) -> MultiPolygon:
    """Axis-aligned rectangle in local metres (offset to the UTM origin) as a MultiPolygon."""
    return MultiPolygon([box(X0 + x0, Y0 + y0, X0 + x1, Y0 + y1)])


def multi(*rects: tuple[float, float, float, float]) -> MultiPolygon:
    return MultiPolygon([box(X0 + a, Y0 + b, X0 + c, Y0 + d) for a, b, c, d in rects])


def to_wgs_wkt(geom_m: BaseGeometry) -> str:
    g = transform(_TO_WGS.transform, geom_m)
    if isinstance(g, Polygon):
        g = MultiPolygon([g])
    return g.wkt


def to_wgs(geom_m: BaseGeometry) -> BaseGeometry:
    return transform(_TO_WGS.transform, geom_m)


@pytest.fixture
def metric_srid() -> int:
    return METRIC_SRID


@pytest.fixture
def cfg() -> dict[str, Any]:
    """Config dict shaped like ``geoqc.config.load_config()`` with fixed test values."""
    return {"crs": {"metric": METRIC_SRID}, "matching": copy.deepcopy(MATCHING_CFG)}


class Db:
    """Small helper around a psycopg connection for inserting synthetic rows."""

    def __init__(self, conn):
        self.conn = conn

    def ref(self, geom_m, *, source: str = "osm", osm_id: int | None = None) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO buildings_ref (osm_id, geom, source) "
                "VALUES (%s, ST_GeomFromText(%s, 4326), %s) RETURNING id",
                (osm_id, to_wgs_wkt(geom_m), source),
            )
            return int(cur.fetchone()["id"])

    def det(self, geom_m, *, score: float = 0.9, model_version: str = "m-test") -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO buildings_det (geom, score, model_version) "
                "VALUES (ST_GeomFromText(%s, 4326), %s, %s) RETURNING id",
                (to_wgs_wkt(geom_m), score, model_version),
            )
            return int(cur.fetchone()["id"])

    def rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any]:
        return self.rows(sql, params)[0]

    def discrepancies(self, model_version: str = "m-test") -> list[dict[str, Any]]:
        return self.rows(
            "SELECT id, class, ref_id, det_id, iou, confidence, status FROM discrepancies "
            "WHERE model_version = %s ORDER BY class, id",
            (model_version,),
        )

    def matches(self, model_version: str = "m-test") -> list[dict[str, Any]]:
        return self.rows(
            "SELECT ref_id, det_id, iou FROM matches WHERE model_version = %s ORDER BY ref_id",
            (model_version,),
        )

    def by_class(self, model_version: str = "m-test") -> dict[str, int]:
        out = {"missing_in_map": 0, "missing_on_image": 0, "geometry_mismatch": 0}
        for r in self.discrepancies(model_version):
            out[r["class"]] += 1
        return out


@pytest.fixture
def db(db_conn) -> Db:
    return Db(db_conn)
