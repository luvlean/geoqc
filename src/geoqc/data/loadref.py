"""Load reference footprints into ``buildings_ref`` (contract §3.7). Idempotent per ``source``."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

_INSERT = """
INSERT INTO buildings_ref (osm_id, geom, tags, source, snapshot_date)
VALUES (
    %(osm_id)s,
    ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s), 4326)), 3)),
    %(tags)s,
    %(source)s,
    %(snapshot_date)s
)
"""


def _iter_rows(features: list[dict[str, Any]], source: str, snapshot_date: str | None):
    seen: set[int] = set()
    for f in features:
        props = f.get("properties") or {}
        osm_id = props.get("osm_id")
        if osm_id is not None:
            osm_id = int(osm_id)
            if osm_id in seen:
                continue
            seen.add(osm_id)
        tags = props.get("tags")
        if tags is None:
            tags = {
                k: v
                for k, v in props.items()
                if k not in ("osm_id", "osm_type", "snapshot_date") and v is not None
            }
        yield {
            "osm_id": osm_id,
            "geom": json.dumps(f["geometry"]),
            "tags": Jsonb(tags),
            "source": source,
            "snapshot_date": props.get("snapshot_date") or snapshot_date,
        }


def load_features(
    conn: psycopg.Connection,
    features: list[dict[str, Any]],
    *,
    source: str,
    snapshot_date: str | None = None,
    batch_size: int = 1000,
) -> int:
    """``DELETE WHERE source=%s`` then bulk insert; returns rows in the table for that source."""
    if source not in ("osm", "microsoft", "google"):
        raise ValueError(f"unsupported source {source!r}")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM buildings_ref WHERE source = %s", (source,))
        batch: list[dict[str, Any]] = []
        for row in _iter_rows(features, source, snapshot_date):
            batch.append(row)
            if len(batch) >= batch_size:
                cur.executemany(_INSERT, batch)
                batch.clear()
        if batch:
            cur.executemany(_INSERT, batch)
        # Drop rows whose geometry collapsed to empty after make_valid/extract.
        cur.execute("DELETE FROM buildings_ref WHERE source = %s AND ST_IsEmpty(geom)", (source,))
        cur.execute("SELECT count(*) AS n FROM buildings_ref WHERE source = %s", (source,))
        n = cur.fetchone()["n"]
    conn.commit()
    return int(n)


def load_geojson_file(
    conn: psycopg.Connection, path: Path, *, source: str, batch_size: int = 1000
) -> int:
    with path.open("r", encoding="utf-8") as fh:
        fc = json.load(fh)
    return load_features(
        conn,
        fc.get("features", []),
        source=source,
        snapshot_date=fc.get("snapshot_date"),
        batch_size=batch_size,
    )


def count_by_source(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, count(*) AS n FROM buildings_ref GROUP BY source ORDER BY source"
        )
        return {r["source"]: int(r["n"]) for r in cur.fetchall()}
