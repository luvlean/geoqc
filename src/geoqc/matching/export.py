"""Export of the review queue to ``reports/discrepancies.geojson`` (docs/CONTRACTS.md §3.8)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg

PROPERTIES = (
    "id",
    "class",
    "confidence",
    "iou",
    "status",
    "ref_osm_id",
    "model_version",
    "created_at",
)


def fetch_features(
    conn: psycopg.Connection,
    *,
    model_version: str | None = None,
    classes: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    min_conf: float = 0.0,
    limit: int | None = None,
    precision: int = 7,
) -> list[dict[str, Any]]:
    """Discrepancies as GeoJSON Feature dicts sorted by confidence DESC (then id)."""
    where = ["x.confidence >= %s"]
    params: list[Any] = [min_conf]
    if model_version:
        where.append("x.model_version = %s")
        params.append(model_version)
    if classes:
        where.append("x.class = ANY(%s)")
        params.append(list(classes))
    if statuses:
        where.append("x.status = ANY(%s)")
        params.append(list(statuses))
    sql = (
        "SELECT x.id, x.class, x.confidence, x.iou, x.status, r.osm_id AS ref_osm_id, "
        "x.model_version, x.created_at, ST_AsGeoJSON(x.geom, %s) AS geometry "
        "FROM discrepancies x LEFT JOIN buildings_ref r ON r.id = x.ref_id "
        f"WHERE {' AND '.join(where)} ORDER BY x.confidence DESC, x.id"
    )
    params = [precision, *params]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    features = []
    for r in rows:
        props = {k: r[k] for k in PROPERTIES}
        props["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        features.append(
            {
                "type": "Feature",
                "id": r["id"],
                "geometry": json.loads(r["geometry"]),
                "properties": props,
            }
        )
    return features


def export_discrepancies(
    conn: psycopg.Connection,
    out_path: Path | str,
    *,
    model_version: str | None = None,
    classes: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    min_conf: float = 0.0,
    limit: int | None = None,
    precision: int = 7,
) -> int:
    """Write the FeatureCollection (EPSG:4326) and return the number of features."""
    features = fetch_features(
        conn,
        model_version=model_version,
        classes=classes,
        statuses=statuses,
        min_conf=min_conf,
        limit=limit,
        precision=precision,
    )
    fc = {
        "type": "FeatureCollection",
        "name": "discrepancies",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    return len(features)
