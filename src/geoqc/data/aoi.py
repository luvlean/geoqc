"""AOI search: OpenAerialMap harvest, filtering/ranking, Overpass density check, AOI polygon.

Procedure (DECISIONS D-001, docs/tasks/02_data.md step 1): query OAM with the Russia bbox first,
then harvest globally; keep RGB orthophotos with GSD <= 0.5 m, footprint >= 5 km², an open
licence and a known date; verify OSM building density with Overpass for the top candidates;
write the ranked list with rejection reasons to ``reports/aoi_candidates.json``.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import requests
from rasterio import features
from rasterio.env import Env
from scipy import ndimage
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import transform as shp_transform

from geoqc.data.crs import utm_epsg_for_bbox
from geoqc.data.osm import fetch_overpass, overpass_bbox

log = logging.getLogger(__name__)

VSICURL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
}


# --------------------------------------------------------------------------- OAM API
def fetch_oam_page(
    api: str,
    *,
    page: int,
    limit: int,
    gsd_to: float,
    bbox: tuple[float, ...] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    params: dict[str, Any] = {"gsd_to": gsd_to, "limit": limit, "page": page}
    if bbox is not None:
        params["bbox"] = ",".join(str(v) for v in bbox)
    r = requests.get(api, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def harvest_oam(
    api: str,
    *,
    gsd_to: float,
    bbox: tuple[float, ...] | None = None,
    page_size: int = 1000,
    max_pages: int = 100,
    retries: int = 3,
) -> tuple[list[dict[str, Any]], int]:
    """Return (all results, ``meta.found``) for the query, paging until exhausted."""
    results: list[dict[str, Any]] = []
    found = 0
    page = 1
    while page <= max_pages:
        data = None
        for attempt in range(retries):
            try:
                data = fetch_oam_page(api, page=page, limit=page_size, gsd_to=gsd_to, bbox=bbox)
                break
            except (requests.RequestException, ValueError) as exc:
                log.warning("OAM page %d attempt %d failed: %s", page, attempt + 1, exc)
                time.sleep(3 * (attempt + 1))
        if data is None:
            break
        found = int((data.get("meta") or {}).get("found") or found)
        batch = data.get("results") or []
        results.extend(batch)
        if len(batch) < page_size or len(results) >= found:
            break
        page += 1
    return results, found


# --------------------------------------------------------------------------- geometry helpers
def bbox_area_km2(bbox: list[float]) -> float:
    minx, miny, maxx, maxy = bbox
    lat = (miny + maxy) / 2.0
    return (maxx - minx) * 111.32 * math.cos(math.radians(lat)) * (maxy - miny) * 110.57


def footprint_area_km2(item: dict[str, Any]) -> float:
    """Footprint area in km² from ``geojson`` (falls back to bbox)."""
    b = item.get("bbox")
    if not b:
        return 0.0
    area = bbox_area_km2(b)
    fp = item.get("geojson")
    if fp:
        try:
            g = shape(fp)
            bb = (b[2] - b[0]) * (b[3] - b[1])
            if bb > 0:
                area *= min(g.area / bb, 1.0)
        except Exception:  # pragma: no cover - malformed footprint
            pass
    return area


def in_bbox(item: dict[str, Any], bbox: tuple[float, ...]) -> bool:
    b = item.get("bbox")
    if not b:
        return False
    cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    return bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]


# --------------------------------------------------------------------------- filtering / ranking
def normalize_license(lic: str | None) -> str:
    return re.sub(r"[\s\-]+", " ", (lic or "")).strip().upper()


def evaluate_item(item: dict[str, Any], crit: dict[str, Any]) -> dict[str, Any]:
    """Apply metadata-only criteria; returns a candidate record with ``rejected`` reason or None."""
    props = item.get("properties") or {}
    lic = props.get("license")
    gsd = item.get("gsd")
    area = footprint_area_km2(item)
    acquired = str(item.get("acquisition_start") or "")[:10]
    title = item.get("title") or ""
    reasons: list[str] = []
    if gsd is None or gsd > crit["gsd_max_m"]:
        reasons.append(f"gsd>{crit['gsd_max_m']}")
    if area < crit["min_image_area_km2"]:
        reasons.append(f"area<{crit['min_image_area_km2']}km2")
    if area > crit["max_image_area_km2"]:
        reasons.append(f"area>{crit['max_image_area_km2']}km2")
    ok_lic = {normalize_license(x) for x in crit["licenses_ok"]}
    if normalize_license(lic) not in ok_lic:
        reasons.append(f"license={lic}")
    if not acquired or acquired[:4] < str(crit["min_year"]):
        reasons.append("date unknown or too old")
    if crit.get("exclude_title_regex") and re.search(crit["exclude_title_regex"], title):
        reasons.append("title marks a non-RGB product (DEM/DSM/...)")
    return {
        "oam_id": item.get("_id"),
        "title": title,
        "provider": item.get("provider"),
        "platform": item.get("platform"),
        "url": item.get("uuid"),
        "gsd_m": gsd,
        "acquired": acquired or None,
        "license": lic,
        "area_km2": round(area, 2),
        "bbox": item.get("bbox"),
        "file_size_mb": round((item.get("file_size") or 0) / 1e6, 1),
        "in_russia_bbox": in_bbox(item, tuple(crit["russia_bbox"])),
        "rejected": "; ".join(reasons) if reasons else None,
    }


def score(c: dict[str, Any]) -> float:
    """Higher is better: Russia first, then recency, finer GSD, moderate area, dense mixed OSM."""
    s = 0.0
    s += 100.0 if c.get("in_russia_bbox") else 0.0
    year = int((c.get("acquired") or "2000")[:4])
    s += max(0, year - 2015) * 3.0
    s += (0.5 - float(c.get("gsd_m") or 0.5)) * 40.0
    area = float(c.get("area_km2") or 0)
    s += 10.0 if 5.0 <= area <= 30.0 else 0.0
    if c.get("header_ok") is False:
        s -= 1000.0
    dens = c.get("osm_buildings_per_5km2")
    if dens is not None:
        s += min(dens / 100.0, 30.0)
        mix = c.get("osm_mix") or {}
        kinds = sum(1 for k in ("apartments", "house", "industrial") if (mix.get(k) or 0) >= 10)
        s += kinds * 10.0
    return round(s, 2)


# --------------------------------------------------------------------------- remote checks
def header_check(url: str, *, erode_m: float = 30.0, simplify_m: float = 15.0) -> dict[str, Any]:
    """Open the remote COG header: RGB uint8? projected CRS? tiled? Plus the derivable AOI area."""
    try:
        with Env(**VSICURL_ENV), rasterio.open(f"/vsicurl/{url}") as ds:
            rgb = ds.count >= 3 and ds.dtypes[0] == "uint8"
            projected = ds.crs is not None and not ds.crs.is_geographic
        aoi_km2 = None
        aoi_bbox_wgs = None
        if rgb and projected:
            try:
                aoi_wgs, aoi_native, _epsg = valid_footprint_polygon(
                    url, erode_m=erode_m, simplify_m=simplify_m
                )
                aoi_km2 = round(aoi_native.area / 1e6, 2)
                aoi_bbox_wgs = [round(v, 6) for v in aoi_wgs.bounds]
            except Exception as exc:  # pragma: no cover - remote failure
                log.warning("footprint derivation failed for %s: %s", url, exc)
        with Env(**VSICURL_ENV), rasterio.open(f"/vsicurl/{url}") as ds:
            return {
                "header_ok": bool(rgb and projected),
                "valid_aoi_km2": aoi_km2,
                "aoi_bbox": aoi_bbox_wgs,
                "bands": ds.count,
                "dtype": ds.dtypes[0],
                "crs": str(ds.crs),
                "width": ds.width,
                "height": ds.height,
                "tiled": bool(ds.profile.get("tiled")),
                "overviews": ds.overviews(1),
                "native_gsd_m": round(float(abs(ds.res[0])), 4),
            }
    except Exception as exc:
        return {"header_ok": False, "error": str(exc)[:200]}


OVERPASS_MIX_QUERY = """[out:json][timeout:{timeout}];
(way["building"]({bbox});relation["building"]({bbox});)->.all; .all out count;
way["building"~"^(apartments|residential|dormitory)$"]({bbox})->.apt; .apt out count;
way["building"~"^(house|detached|semidetached_house|terrace|bungalow)$"]({bbox})->.house; .house out count;
way["building"~"^(industrial|warehouse|factory|garages|garage)$"]({bbox})->.ind; .ind out count;"""


def overpass_density(
    bbox: list[float],
    endpoints: list[str],
    *,
    timeout_s: int = 240,
    retries: int = 2,
    user_agent: str = "geoqc/0.1",
) -> dict[str, Any]:
    """Total OSM buildings and a coarse type mix for a bbox; ``None`` values when Overpass fails."""
    q = OVERPASS_MIX_QUERY.format(timeout=min(timeout_s, 180), bbox=overpass_bbox(tuple(bbox)))
    try:
        data = fetch_overpass(
            q, endpoints, timeout_s=timeout_s, retries=retries, backoff_s=5, user_agent=user_agent
        )
        counts = [int(e["tags"]["total"]) for e in data["elements"] if e.get("type") == "count"]
        total, apt, house, ind = (counts + [0, 0, 0, 0])[:4]
        area = bbox_area_km2(bbox)
        return {
            "osm_buildings": total,
            "osm_buildings_per_5km2": round(total / area * 5.0, 1) if area > 0 else None,
            "osm_mix": {"apartments": apt, "house": house, "industrial": ind},
            "osm_snapshot": (data.get("osm3s") or {}).get("timestamp_osm_base"),
        }
    except Exception as exc:
        return {
            "osm_buildings": None,
            "osm_buildings_per_5km2": None,
            "osm_mix": None,
            "osm_error": str(exc)[:200],
        }


# --------------------------------------------------------------------------- AOI polygon
def largest_inscribed_rectangle(mask: np.ndarray) -> tuple[int, int, int, int, int]:
    """Largest all-True axis-aligned rectangle in ``mask``: (area_px, r0, c0, r1, c1) inclusive."""
    h, w = mask.shape
    heights = np.zeros(w, dtype=int)
    best = (0, 0, 0, -1, -1)
    for r in range(h):
        heights = np.where(mask[r], heights + 1, 0)
        stack: list[tuple[int, int]] = []
        for c in range(w + 1):
            cur = int(heights[c]) if c < w else 0
            start = c
            while stack and stack[-1][1] >= cur:
                s0, hh = stack.pop()
                area = hh * (c - s0)
                if area > best[0]:
                    best = (area, r - hh + 1, s0, r, c - 1)
                start = s0
            stack.append((start, cur))
    return best


def valid_footprint_polygon(
    url: str,
    *,
    erode_m: float = 30.0,
    simplify_m: float = 15.0,
    target_px: int = 3500,
    max_area_km2: float | None = None,
) -> tuple[Polygon, Polygon, int]:
    """Largest valid-data polygon of a remote COG, eroded and simplified; returns (aoi_wgs84, aoi_native, epsg).

    Reads the dataset mask at a coarse overview so that a 10 km² image costs a few MB. When the
    eroded footprint exceeds ``max_area_km2`` the largest inscribed rectangle is used instead
    (and, if still too large, it is shrunk around its centre).
    """
    with Env(**VSICURL_ENV), rasterio.open(f"/vsicurl/{url}") as ds:
        f = max(1, int(math.ceil(max(ds.width, ds.height) / target_px)))
        w, h = max(1, ds.width // f), max(1, ds.height // f)
        mask = ds.read_masks(1, out_shape=(h, w)) > 0
        res = float(abs(ds.res[0])) * ds.width / w
        tr = rasterio.Affine(res, 0, ds.bounds.left, 0, -res, ds.bounds.top)
        mask = ndimage.binary_opening(mask, iterations=3)
        polys = [
            shape(g)
            for g, v in features.shapes(mask.astype(np.uint8), mask=mask, transform=tr)
            if v == 1
        ]
        if not polys:
            raise RuntimeError("no valid pixels in remote image mask")
        fp = max(polys, key=lambda p: p.area)
        crs = ds.crs
        if crs is None or crs.is_geographic:
            raise RuntimeError("footprint derivation expects a projected source CRS")
        er = fp.buffer(-erode_m).simplify(simplify_m)
        if er.geom_type == "MultiPolygon":
            er = max(er.geoms, key=lambda p: p.area)
        er = Polygon(er.exterior.coords).buffer(0)
        if max_area_km2 is not None and er.area / 1e6 > max_area_km2:
            eroded_mask = ndimage.binary_erosion(mask, iterations=max(1, int(erode_m / res)))
            _a, r0, c0, r1, c1 = largest_inscribed_rectangle(eroded_mask)
            x0, y1 = tr * (c0, r0)
            x1, y0 = tr * (c1 + 1, r1 + 1)
            er = box(x0, y0, x1, y1)
            if er.area / 1e6 > max_area_km2:
                k = math.sqrt(max_area_km2 * 1e6 / er.area)
                cx, cy = er.centroid.x, er.centroid.y
                hw, hh = (x1 - x0) * k / 2.0, (y1 - y0) * k / 2.0
                er = box(cx - hw, cy - hh, cx + hw, cy + hh)
    import pyproj

    to_wgs = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    aoi_wgs = shp_transform(to_wgs, er)
    epsg = utm_epsg_for_bbox(aoi_wgs.bounds)
    if crs.to_epsg() != epsg:
        to_utm = pyproj.Transformer.from_crs(crs, f"EPSG:{epsg}", always_xy=True).transform
        er = shp_transform(to_utm, er)
    return aoi_wgs, er, epsg


def write_aoi_geojson(
    aoi_wgs84: Polygon,
    path: Path,
    *,
    name: str,
    source: str,
    image_id: str,
    utm_epsg: int,
    extra: dict[str, Any],
) -> Path:
    props = {"name": name, "source": source, "image_id": image_id, "utm_epsg": utm_epsg}
    props.update(extra)
    fc = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": mapping(aoi_wgs84), "properties": props}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False, indent=1)
    return path


def load_aoi(path: Path) -> tuple[Polygon, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        fc = json.load(fh)
    feat = fc["features"][0] if fc.get("type") == "FeatureCollection" else fc
    return shape(feat["geometry"]), dict(feat.get("properties") or {})


def aoi_area_km2(aoi_wgs84: Polygon, epsg: int) -> float:
    import pyproj

    tr = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
    return shp_transform(tr, aoi_wgs84).area / 1e6


# --------------------------------------------------------------------------- driver
def search_aoi(
    crit: dict[str, Any],
    *,
    overpass_endpoints: list[str],
    overpass_timeout_s: int,
    user_agent: str,
    report_path: Path,
    skip_global: bool = False,
    force_choice: str | None = None,
) -> dict[str, Any]:
    """Run the whole procedure and write ``reports/aoi_candidates.json``; return the report dict."""
    api = crit["oam_api"]
    t0 = time.time()
    ru_bbox = tuple(crit["russia_bbox"])
    ru_results, ru_found = harvest_oam(
        api, gsd_to=crit["gsd_max_m"], bbox=ru_bbox, page_size=crit["page_size"]
    )
    log.info("OAM Russia bbox: found=%d fetched=%d", ru_found, len(ru_results))
    glob_results: list[dict[str, Any]] = []
    glob_found = 0
    if not skip_global:
        glob_results, glob_found = harvest_oam(
            api, gsd_to=crit["gsd_max_m"], page_size=crit["page_size"]
        )
        log.info("OAM global: found=%d fetched=%d", glob_found, len(glob_results))
    by_id: dict[str, dict[str, Any]] = {}
    for it in ru_results + glob_results:
        if it.get("_id"):
            by_id[it["_id"]] = it
    evaluated = [evaluate_item(it, crit) for it in by_id.values()]
    passed = [c for c in evaluated if c["rejected"] is None]
    for c in passed:
        c["score"] = score(c)
    passed.sort(key=lambda c: -c["score"])
    # Remote header check for the top N, then Overpass density for the top M of those.
    min_aoi = float(crit.get("min_aoi_area_km2", crit["min_image_area_km2"]))
    for c in passed[: crit["header_check_top_n"]]:
        c.update(
            header_check(
                c["url"], erode_m=crit.get("erode_m", 30.0), simplify_m=crit.get("simplify_m", 15.0)
            )
        )
        if not c.get("header_ok"):
            c["rejected"] = "remote header: not an RGB uint8 projected GeoTIFF"
        elif c.get("valid_aoi_km2") is not None and c["valid_aoi_km2"] < min_aoi:
            c["rejected"] = (
                f"contiguous valid footprint {c['valid_aoi_km2']} km2 < {min_aoi} km2 after {crit.get('erode_m', 30.0)} m erosion"
            )
        c["score"] = score(c)
    passed.sort(key=lambda c: -c["score"])
    checked = 0
    for c in passed:
        if checked >= crit["overpass_check_top_n"]:
            break
        if c.get("rejected"):
            continue
        c.update(
            overpass_density(
                c.get("aoi_bbox") or c["bbox"],
                overpass_endpoints,
                timeout_s=overpass_timeout_s,
                user_agent=user_agent,
            )
        )
        checked += 1
        dens = c.get("osm_buildings_per_5km2")
        if dens is not None and dens < crit["min_osm_buildings_per_5km2"]:
            c["rejected"] = f"osm density {dens}/5km2 < {crit['min_osm_buildings_per_5km2']}"
        c["score"] = score(c)
    passed.sort(key=lambda c: (c.get("rejected") is not None, -c["score"]))
    for rank, c in enumerate(passed, 1):
        c["rank"] = rank
    rejected_summary: dict[str, int] = {}
    for c in evaluated:
        if c["rejected"]:
            key = c["rejected"].split(";")[0].strip()
            rejected_summary[key] = rejected_summary.get(key, 0) + 1
    chosen = next((c for c in passed if not c.get("rejected") and c.get("osm_buildings")), None)
    if force_choice:
        forced = next((c for c in evaluated if c["oam_id"] == force_choice), None)
        if forced is None:
            raise ValueError(f"OAM id {force_choice} not in harvested results")
        if forced.get("osm_buildings") is None:
            forced.update(
                header_check(
                    forced["url"],
                    erode_m=crit.get("erode_m", 30.0),
                    simplify_m=crit.get("simplify_m", 15.0),
                )
            )
            forced.update(
                overpass_density(
                    forced.get("aoi_bbox") or forced["bbox"],
                    overpass_endpoints,
                    timeout_s=overpass_timeout_s,
                    user_agent=user_agent,
                )
            )
        forced["forced_choice"] = True
        chosen = forced
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "elapsed_s": round(time.time() - t0, 1),
        "criteria": crit,
        "queries": {
            "russia_bbox": {"bbox": list(ru_bbox), "found": ru_found, "fetched": len(ru_results)},
            "global": {"found": glob_found, "fetched": len(glob_results)},
        },
        "n_unique": len(evaluated),
        "n_pass_metadata_filters": len(
            [c for c in evaluated if c["rejected"] is None or c.get("score") is not None]
        ),
        "rejected_summary": dict(sorted(rejected_summary.items(), key=lambda kv: -kv[1])),
        "chosen": chosen,
        "candidates": passed[:200],
        "maxar_open_data_fallback": {
            "catalog": "https://maxar-opendata.s3.amazonaws.com/events/catalog.json",
            "used": chosen is None,
            "license": "CC BY-NC 4.0",
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    return report


def aoi_bbox(aoi_wgs84: Polygon) -> tuple[float, float, float, float]:
    return tuple(round(v, 7) for v in aoi_wgs84.bounds)  # type: ignore[return-value]


def rect_from_bbox(bbox: tuple[float, float, float, float]) -> Polygon:
    return box(*bbox)
