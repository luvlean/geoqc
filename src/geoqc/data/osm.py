"""OSM buildings via Overpass: query with retries and cache, polygonization of ways/relations.

Output contract: ``data/interim/osm_buildings.geojson`` with ``osm_id, osm_type, tags,
snapshot_date`` (docs/tasks/02_data.md step 5). Ways get their positive id; multipolygon
relations get ``-id`` so that ``osm_id`` stays unique across both element types.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from shapely.geometry import LineString, MultiPolygon, Polygon, mapping
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, polygonize, unary_union
from shapely.validation import make_valid

log = logging.getLogger(__name__)

DEFAULT_QUERY = (
    '[out:json][timeout:180]; (way["building"]({bbox}); relation["building"]({bbox});); out geom;'
)


def overpass_bbox(bbox_wgs84: tuple[float, float, float, float]) -> str:
    """Overpass wants ``south,west,north,east``; the project passes ``minx,miny,maxx,maxy``."""
    minx, miny, maxx, maxy = bbox_wgs84
    return f"{miny:.7f},{minx:.7f},{maxy:.7f},{maxx:.7f}"


def buffer_bbox_m(
    bbox_wgs84: tuple[float, float, float, float], meters: float
) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = bbox_wgs84
    lat = (miny + maxy) / 2.0
    dlat = meters / 111_320.0
    dlon = meters / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return (minx - dlon, miny - dlat, maxx + dlon, maxy + dlat)


def fetch_overpass(
    query: str,
    endpoints: list[str],
    *,
    timeout_s: int = 240,
    retries: int = 3,
    backoff_s: float = 10.0,
    user_agent: str = "geoqc/0.1",
) -> dict[str, Any]:
    """POST ``query`` to the first endpoint that answers 200 with JSON; rotate endpoints on failure."""
    last: Exception | None = None
    for attempt in range(retries):
        for ep in endpoints:
            try:
                t0 = time.time()
                r = requests.post(
                    ep,
                    data={"data": query},
                    timeout=timeout_s,
                    headers={"User-Agent": user_agent},
                )
                if r.status_code in (429, 504, 502, 503):
                    log.warning("overpass %s -> %s, rotating", ep, r.status_code)
                    last = RuntimeError(f"{ep}: HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                data = r.json()
                if "elements" not in data:
                    raise RuntimeError(f"{ep}: no 'elements' in response")
                log.info(
                    "overpass ok via %s in %.1fs (%d elements)",
                    ep,
                    time.time() - t0,
                    len(data["elements"]),
                )
                return data
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                log.warning("overpass %s failed: %s", ep, exc)
                last = exc
        time.sleep(backoff_s * (attempt + 1))
    raise RuntimeError(f"Overpass failed after {retries} rounds: {last}")


def snapshot_date_of(data: dict[str, Any]) -> str:
    ts = (data.get("osm3s") or {}).get("timestamp_osm_base")
    if ts:
        return ts[:10]
    return datetime.now(UTC).date().isoformat()


def _way_polygon(el: dict[str, Any]) -> Polygon | None:
    geom = el.get("geometry") or []
    if len(geom) < 4:
        return None
    coords = [(p["lon"], p["lat"]) for p in geom]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    poly = Polygon(coords)
    if poly.is_empty:
        return None
    return poly


def _rings_from_members(members: list[dict[str, Any]], role: str) -> list[Polygon]:
    """Merge way members with the given role into closed rings -> polygons."""
    lines: list[LineString] = []
    for m in members:
        if m.get("type") != "way" or (m.get("role") or "outer") != role:
            continue
        geom = m.get("geometry") or []
        if len(geom) < 2:
            continue
        lines.append(LineString([(p["lon"], p["lat"]) for p in geom]))
    if not lines:
        return []
    merged = linemerge(lines) if len(lines) > 1 else lines[0]
    return [p for p in polygonize(merged) if not p.is_empty]


def relation_polygon(el: dict[str, Any]) -> Polygon | MultiPolygon | None:
    """Assemble a multipolygon relation from ``out geom`` members (outer minus inner rings)."""
    members = el.get("members") or []
    outers = _rings_from_members(members, "outer")
    if not outers:
        return None
    inners = _rings_from_members(members, "inner")
    geom = unary_union(outers)
    if inners:
        geom = geom.difference(unary_union(inners))
    geom = make_valid(geom)
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        if not polys:
            return None
        geom = unary_union(polys)
    if geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    return geom


def polygonize_elements(
    elements: list[dict[str, Any]], *, snapshot_date: str, min_area_m2: float = 0.0
) -> list[dict[str, Any]]:
    """Turn Overpass ``out geom`` elements into GeoJSON features with the module contract."""
    feats: list[dict[str, Any]] = []
    seen: set[int] = set()
    for el in elements:
        tags = el.get("tags") or {}
        if tags.get("building") in (None, "no"):
            continue
        if el["type"] == "way":
            geom = _way_polygon(el)
            osm_id = int(el["id"])
        elif el["type"] == "relation":
            geom = relation_polygon(el)
            osm_id = -int(el["id"])
        else:
            continue
        if geom is None or osm_id in seen:
            continue
        geom = make_valid(geom)
        if geom.geom_type == "GeometryCollection":
            parts = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
            if not parts:
                continue
            geom = unary_union(parts)
        if geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        if min_area_m2 > 0 and _approx_area_m2(geom) < min_area_m2:
            continue
        if geom.geom_type == "Polygon":
            geom = orient(geom)
        seen.add(osm_id)
        feats.append(
            {
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": {
                    "osm_id": osm_id,
                    "osm_type": el["type"],
                    "tags": tags,
                    "snapshot_date": snapshot_date,
                },
            }
        )
    return feats


def _approx_area_m2(geom: Polygon | MultiPolygon) -> float:
    lat = geom.centroid.y
    kx = 111_320.0 * math.cos(math.radians(lat))
    ky = 110_574.0
    return geom.area * kx * ky


def write_geojson(
    features: list[dict[str, Any]], path: Path, extra: dict[str, Any] | None = None
) -> Path:
    fc: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    if extra:
        fc.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False)
    return path


def latest_cache(cache_dir: Path) -> Path | None:
    files = sorted(cache_dir.glob("osm_*.json"))
    return files[-1] if files else None


def download_osm_buildings(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    cache_dir: Path,
    output: Path,
    endpoints: list[str],
    query_template: str = DEFAULT_QUERY,
    bbox_buffer_m: float = 0.0,
    min_area_m2: float = 0.0,
    timeout_s: int = 240,
    retries: int = 3,
    backoff_s: float = 10.0,
    user_agent: str = "geoqc/0.1",
    force: bool = False,
) -> dict[str, Any]:
    """Fetch (or reuse the cached) Overpass response and write the polygonized GeoJSON.

    Returns ``{"count", "snapshot_date", "query", "cache", "output"}`` for the manifest.
    """
    bbox = buffer_bbox_m(bbox_wgs84, bbox_buffer_m) if bbox_buffer_m else bbox_wgs84
    query = query_template.format(bbox=overpass_bbox(bbox))
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = None if force else latest_cache(cache_dir)
    if cache is not None:
        with cache.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("_geoqc_query") != query:
            log.info("cached Overpass response has a different bbox/query; refetching")
            cache = None
    if cache is None:
        data = fetch_overpass(
            query,
            endpoints,
            timeout_s=timeout_s,
            retries=retries,
            backoff_s=backoff_s,
            user_agent=user_agent,
        )
        data["_geoqc_query"] = query
        data["_geoqc_fetched_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        cache = cache_dir / f"osm_{snapshot_date_of(data)}.json"
        with cache.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    snapshot = snapshot_date_of(data)
    feats = polygonize_elements(data["elements"], snapshot_date=snapshot, min_area_m2=min_area_m2)
    write_geojson(
        feats,
        output,
        extra={
            "snapshot_date": snapshot,
            "license": "ODbL",
            "attribution": "© OpenStreetMap contributors (ODbL)",
        },
    )
    return {
        "count": len(feats),
        "snapshot_date": snapshot,
        "query": query,
        "cache": str(cache),
        "output": str(output),
    }
