"""Service metadata: ``/health``, ``/stats``, ``/config/map`` and the Esri basemap proxy."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

import requests
from fastapi import APIRouter, Depends, HTTPException, Response
from shapely.geometry import shape
from shapely.ops import unary_union

from geoqc.api.deps import DISCREPANCY_CLASSES, DISCREPANCY_STATUSES, AppContext, get_ctx
from geoqc.api.routes.tiles import validate_tile
from geoqc.api.schemas import AoiInfo, Basemap, Health, ImageInfo, MapConfig, Stats

log = logging.getLogger("geoqc.api.meta")
router = APIRouter(tags=["meta"])

OSM_ATTRIBUTION = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, ODbL'
)


@router.get("/health", response_model=Health, summary="Liveness + DB check")
def health(ctx: Annotated[AppContext, Depends(get_ctx)]) -> Health:
    ok = ctx.db_ok()
    return Health(status="ok" if ok else "degraded", db=ok)


@router.get("/stats", response_model=Stats, summary="Counts by class and status")
def stats(ctx: Annotated[AppContext, Depends(get_ctx)]) -> Stats:
    by_class = dict.fromkeys(DISCREPANCY_CLASSES, 0)
    by_status = dict.fromkeys(DISCREPANCY_STATUSES, 0)
    by_class_status = {c: dict.fromkeys(DISCREPANCY_STATUSES, 0) for c in DISCREPANCY_CLASSES}
    with ctx.connection() as conn:
        rows = conn.execute(
            "SELECT class, status, count(*) AS n FROM discrepancies GROUP BY class, status"
        ).fetchall()
        versions = conn.execute(
            "SELECT DISTINCT model_version FROM discrepancies "
            "WHERE model_version IS NOT NULL ORDER BY 1"
        ).fetchall()
        n_ref = int(conn.execute("SELECT count(*) AS n FROM buildings_ref").fetchone()["n"])
        if ctx.has_matching_tables(conn):
            # D-012: NMS-suppressed detections stay in buildings_det but are not counted.
            det_row = conn.execute(
                "SELECT count(*) FILTER (WHERE s.det_id IS NULL) AS active,"
                " count(*) FILTER (WHERE s.det_id IS NOT NULL) AS suppressed"
                " FROM buildings_det d LEFT JOIN det_suppressed s"
                " ON s.det_id = d.id AND s.model_version = d.model_version"
            ).fetchone()
            n_det, n_sup = int(det_row["active"]), int(det_row["suppressed"])
        else:
            n_det = int(conn.execute("SELECT count(*) AS n FROM buildings_det").fetchone()["n"])
            n_sup = 0
    total = 0
    for row in rows:
        n = int(row["n"])
        total += n
        by_class[row["class"]] = by_class.get(row["class"], 0) + n
        by_status[row["status"]] = by_status.get(row["status"], 0) + n
        by_class_status.setdefault(row["class"], {})[row["status"]] = n
    return Stats(
        total=total,
        by_class=by_class,
        by_status=by_status,
        by_class_status=by_class_status,
        model_versions=[v["model_version"] for v in versions],
        buildings={"ref": n_ref, "det": n_det, "det_suppressed": n_sup},
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("cannot read %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def aoi_info_from_geojson(data: dict[str, Any]) -> AoiInfo:
    """Center/bbox of a GeoJSON Geometry, Feature or FeatureCollection (EPSG:4326)."""
    kind = data.get("type")
    if kind == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in data.get("features", []) if f.get("geometry")]
        props = (data.get("features") or [{}])[0].get("properties") or {}
    elif kind == "Feature":
        geoms = [shape(data["geometry"])] if data.get("geometry") else []
        props = data.get("properties") or {}
    else:
        geoms = [shape(data)]
        props = {}
    if not geoms:
        return AoiInfo()
    geom = unary_union(geoms)
    minx, miny, maxx, maxy = geom.bounds
    center = geom.centroid
    name = props.get("name") or data.get("name")
    return AoiInfo(
        name=str(name) if name else None,
        center=[round(center.x, 7), round(center.y, 7)],
        bbox=[round(v, 7) for v in (minx, miny, maxx, maxy)],
    )


def build_map_config(ctx: AppContext) -> MapConfig:
    notes: list[str] = []
    aoi = AoiInfo()
    aoi_path = ctx.paths["aoi"]
    aoi_data = _read_json(aoi_path) if aoi_path.exists() else None
    if aoi_data is None:
        notes.append(f"{aoi_path.name} not found or unreadable: AOI center/bbox are null")
    else:
        try:
            aoi = aoi_info_from_geojson(aoi_data)
        except Exception as exc:  # malformed geometry must not take the API down
            log.warning("cannot parse %s: %s", aoi_path, exc)
            notes.append(f"{aoi_path.name} could not be parsed: {exc}")

    manifest_path = ctx.paths["manifest"]
    manifest = _read_json(manifest_path) if manifest_path.exists() else None
    image: ImageInfo | None = None
    image_attr: str | None = None
    osm_snapshot: str | None = None
    if manifest is None:
        notes.append(f"{manifest_path.name} not found: imagery attribution unknown")
    else:
        m_aoi = manifest.get("aoi") or {}
        if aoi.name is None and m_aoi.get("name"):
            aoi.name = str(m_aoi["name"])
        if aoi.bbox is None and m_aoi.get("bbox_wgs84"):
            bb = [float(v) for v in m_aoi["bbox_wgs84"]]
            aoi.bbox = bb
            aoi.center = [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2]
        aoi.area_km2 = m_aoi.get("area_km2")
        aoi.utm_epsg = m_aoi.get("utm_epsg")
        m_img = manifest.get("image") or {}
        if m_img:
            image = ImageInfo(
                source=m_img.get("source"),
                id=m_img.get("id"),
                acquired=m_img.get("acquired"),
                license=m_img.get("license"),
                gsd_m=m_img.get("gsd_m"),
                url=m_img.get("url"),
            )
            image_attr = m_img.get("attribution") or (
                f"{m_img.get('source', 'imagery')} {m_img.get('id', '')}".strip()
            )
            if m_img.get("license"):
                image_attr = f"{image_attr} ({m_img['license']})"
        osm_snapshot = (manifest.get("osm") or {}).get("snapshot_date")

    bm_cfg = ctx.config["basemaps"]
    basemaps = [
        Basemap(
            key="osm",
            title="OSM Standard",
            url=bm_cfg["osm"]["url"],
            attribution=bm_cfg["osm"].get("attribution", OSM_ATTRIBUTION),
            max_zoom=int(bm_cfg["osm"].get("max_zoom", 19)),
        )
    ]
    imagery_available = ctx.paths["cog"].exists()
    if imagery_available:
        basemaps.append(
            Basemap(
                key="imagery",
                title="Снимок (COG)",
                url="/imagery/{z}/{x}/{y}.png",
                attribution=image_attr or "AOI imagery",
                max_zoom=int(ctx.config["mvt"]["max_zoom"]),
            )
        )
    else:
        notes.append(f"{ctx.paths['cog'].name} not found: imagery basemap disabled")
    esri_attr = bm_cfg["esri"]["attribution"]
    if ctx.esri_enabled:
        basemaps.append(
            Basemap(
                key="esri",
                title="Esri World Imagery",
                url="/basemap/esri/{z}/{y}/{x}",
                attribution=esri_attr,
                max_zoom=int(bm_cfg["esri"].get("max_zoom", 19)),
            )
        )
    return MapConfig(
        aoi=aoi,
        image=image,
        attribution={
            "osm": OSM_ATTRIBUTION,
            "image": image_attr,
            "esri": esri_attr if ctx.esri_enabled else None,
        },
        basemaps=basemaps,
        imagery_available=imagery_available,
        esri_enabled=ctx.esri_enabled,
        review_tools=ctx.review_enabled,
        osm_snapshot_date=osm_snapshot,
        mvt_layers={n: int(v.get("min_zoom", 0)) for n, v in ctx.config["mvt"]["layers"].items()},
        default_limit=int(ctx.config["pagination"]["default_limit"]),
        max_limit=int(ctx.config["pagination"]["max_limit"]),
        note="; ".join(notes) or None,
    )


@router.get("/config/map", response_model=MapConfig, summary="Map bootstrap config")
def config_map(ctx: Annotated[AppContext, Depends(get_ctx)]) -> MapConfig:
    """AOI center/bbox, attributions (OSM ODbL, image source, Esri), basemaps and flags."""
    return build_map_config(ctx)


@router.get(
    "/basemap/esri/{z}/{y}/{x}", summary="Esri World Imagery proxy (token stays server-side)"
)
def esri_basemap(z: int, y: int, x: int, ctx: Annotated[AppContext, Depends(get_ctx)]) -> Response:
    """Proxies one tile with ``ARCGIS_API_KEY`` appended on the server (DECISIONS.md D-002)."""
    if not ctx.esri_enabled:
        raise HTTPException(status_code=404, detail="Esri basemap disabled: ARCGIS_API_KEY not set")
    esri_cfg = ctx.config["basemaps"]["esri"]
    validate_tile(z, x, y, int(esri_cfg.get("max_zoom", 19)))
    url = esri_cfg["upstream"].format(z=z, y=y, x=x)
    try:
        upstream = requests.get(
            url, params={"token": ctx.arcgis_api_key}, timeout=float(esri_cfg.get("timeout_s", 10))
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Esri upstream error: {exc}") from exc
    if upstream.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"Esri upstream returned HTTP {upstream.status_code}"
        )
    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": ctx.cache_header("basemap")},
    )
