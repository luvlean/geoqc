"""Secondary building footprints: Microsoft GlobalMLBuildingFootprints or Google Open Buildings.

Both datasets are looked up by spatial index (Bing quadkey / S2 tiles) and filtered to the AOI
bbox. When neither covers the AOI the caller records ``secondary.source = null`` (honest
fallback required by docs/tasks/02_data.md step 6).
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
from pathlib import Path
from typing import Any

import mercantile
import requests
from shapely.geometry import box, mapping, shape
from shapely.validation import make_valid

log = logging.getLogger(__name__)


def quadkeys_for_bbox(bbox_wgs84: tuple[float, float, float, float], level: int = 9) -> list[str]:
    minx, miny, maxx, maxy = bbox_wgs84
    return sorted(
        {mercantile.quadkey(t) for t in mercantile.tiles(minx, miny, maxx, maxy, [level])}
    )


def microsoft_links(csv_url: str, timeout: int = 120) -> list[dict[str, str]]:
    r = requests.get(csv_url, timeout=timeout)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return list(reader)


def microsoft_candidates(links: list[dict[str, str]], quadkeys: list[str]) -> list[dict[str, str]]:
    keys = set(quadkeys)
    return [row for row in links if str(row.get("QuadKey", "")).strip() in keys]


def _load_geojsonl(content: bytes) -> list[dict[str, Any]]:
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    feats: list[dict[str, Any]] = []
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("type") == "Feature":
            feats.append(obj)
        elif obj.get("type") in ("Polygon", "MultiPolygon"):
            feats.append({"type": "Feature", "geometry": obj, "properties": {}})
    return feats


def fetch_microsoft(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    links_csv_url: str,
    level: int = 9,
    timeout: int = 300,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (features clipped to bbox, urls used). Empty list when no quadkey is published."""
    qks = quadkeys_for_bbox(bbox_wgs84, level)
    links = microsoft_links(links_csv_url)
    rows = microsoft_candidates(links, qks)
    if not rows:
        log.info("Microsoft footprints: no quadkeys %s in dataset-links.csv", qks)
        return [], []
    aoi = box(*bbox_wgs84)
    out: list[dict[str, Any]] = []
    urls: list[str] = []
    for row in rows:
        url = row["Url"]
        urls.append(url)
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        for f in _load_geojsonl(r.content):
            g = shape(f["geometry"])
            if not g.intersects(aoi):
                continue
            g = make_valid(g)
            if g.is_empty or g.geom_type not in ("Polygon", "MultiPolygon"):
                continue
            props = f.get("properties") or {}
            out.append(
                {
                    "type": "Feature",
                    "geometry": mapping(g),
                    "properties": {
                        "source": "microsoft",
                        "location": row.get("Location"),
                        "quadkey": row.get("QuadKey"),
                        "height": props.get("height"),
                        "confidence": props.get("confidence"),
                    },
                }
            )
    return out, urls


def google_coverage(
    bbox_wgs84: tuple[float, float, float, float], tiles_url: str, timeout: int = 120
) -> list[str]:
    """S2 cell tokens of Google Open Buildings that intersect the bbox (empty if not covered)."""
    try:
        r = requests.get(tiles_url, timeout=timeout)
        r.raise_for_status()
        fc = r.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("Google Open Buildings tiles index unavailable: %s", exc)
        return []
    aoi = box(*bbox_wgs84)
    tokens = []
    for f in fc.get("features", []):
        try:
            if shape(f["geometry"]).intersects(aoi):
                tokens.append(str(f["properties"].get("tile_id") or f["properties"].get("id")))
        except Exception:  # pragma: no cover - malformed feature
            continue
    return tokens


def fetch_google(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    tokens: list[str],
    base_url: str,
    timeout: int = 600,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Download Google Open Buildings CSV.gz tiles and keep polygons intersecting the bbox."""
    aoi = box(*bbox_wgs84)
    out: list[dict[str, Any]] = []
    urls: list[str] = []
    from shapely import wkt

    for tok in tokens:
        url = f"{base_url}/{tok}_buildings.csv.gz"
        urls.append(url)
        r = requests.get(url, timeout=timeout)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        text = gzip.decompress(r.content).decode("utf-8")
        for row in csv.DictReader(io.StringIO(text)):
            g = wkt.loads(row["geometry"])
            if not g.intersects(aoi):
                continue
            out.append(
                {
                    "type": "Feature",
                    "geometry": mapping(g),
                    "properties": {
                        "source": "google",
                        "confidence": float(row.get("confidence", 0) or 0),
                        "plus_code": row.get("full_plus_code"),
                    },
                }
            )
    return out, urls


def fetch_secondary(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    output: Path,
    microsoft_links_csv: str,
    microsoft_level: int,
    google_tiles_url: str,
    google_base_url: str,
) -> dict[str, Any]:
    """Try Microsoft, then Google; write GeoJSON and return manifest-ready ``secondary`` section."""
    feats, urls = fetch_microsoft(
        bbox_wgs84, links_csv_url=microsoft_links_csv, level=microsoft_level
    )
    result: dict[str, Any]
    if feats:
        result = {
            "source": "microsoft",
            "url": urls[0] if len(urls) == 1 else urls,
            "count": len(feats),
            "license": "ODbL",
        }
    else:
        tokens = google_coverage(bbox_wgs84, google_tiles_url)
        gfeats: list[dict[str, Any]] = []
        gurls: list[str] = []
        if tokens:
            gfeats, gurls = fetch_google(bbox_wgs84, tokens=tokens, base_url=google_base_url)
        if gfeats:
            feats = gfeats
            result = {"source": "google", "url": gurls, "count": len(feats), "license": "CC-BY-4.0"}
        else:
            result = {
                "source": None,
                "url": None,
                "count": 0,
                "license": None,
                "note": "neither Microsoft GlobalMLBuildingFootprints nor Google Open Buildings covers the AOI",
            }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats, "secondary": result}, fh)
    result["output"] = str(output)
    return result
