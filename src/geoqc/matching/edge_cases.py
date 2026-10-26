"""Edge cases handled before / around matching: duplicate detections, multipart detections, AOI edge.

All heavy lifting is in ``sql/002_matching.sql``; this module is the thin Python wrapper plus
AOI loading. The AOI edge flag itself is computed by ``geoqc_discrepancy_features`` and applied as
a confidence multiplier in :mod:`geoqc.matching.scoring`.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
from shapely.geometry import mapping, shape
from shapely.ops import unary_union


def dedup_detections(
    conn: psycopg.Connection, model_version: str, metric_srid: int, dedup_iou: float
) -> int:
    """NMS among detections of ``model_version``; suppressed ids go to ``det_suppressed``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT geoqc_dedup_detections(%s, %s, %s) AS n",
            (model_version, metric_srid, dedup_iou),
        )
        return int(cur.fetchone()["n"])


def suppress_small_detections(
    conn: psycopg.Connection,
    model_version: str,
    metric_srid: int,
    min_area_m2: float,
    nested_cover: float,
    nested_max_ratio: float,
    iou_match: float,
) -> int:
    """Suppress detections below ``min_area_m2`` and small ones nested in a larger detection.

    A nested detection that matches a reference building (IoU >= ``iou_match``) is kept.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT geoqc_suppress_small_detections(%s, %s, %s, %s, %s, %s) AS n",
            (model_version, metric_srid, min_area_m2, nested_cover, nested_max_ratio, iou_match),
        )
        return int(cur.fetchone()["n"])


def split_multipart_detections(conn: psycopg.Connection, model_version: str) -> int:
    """Replace multipart detections by one row per part. Returns the number of rows split."""
    with conn.cursor() as cur:
        cur.execute("SELECT geoqc_split_multipart_detections(%s) AS n", (model_version,))
        return int(cur.fetchone()["n"])


def load_aoi_geojson(path: Path | str | None) -> str | None:
    """Return the AOI as a GeoJSON *geometry* string (EPSG:4326) or None when the file is absent.

    Accepts a bare geometry, a Feature or a FeatureCollection (features are unioned).
    """
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    kind = data.get("type")
    if kind == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in data.get("features", []) if f.get("geometry")]
        if not geoms:
            return None
        geom = unary_union(geoms)
    elif kind == "Feature":
        geom = shape(data["geometry"])
    else:
        geom = shape(data)
    if geom.is_empty:
        return None
    return json.dumps(mapping(geom))
