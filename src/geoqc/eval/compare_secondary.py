"""Detections vs the secondary footprint source (Microsoft/Google) as an alternative detector.

Both are evaluated against the manual test labels with the same 1:1 matching; additionally the
detections are matched against the secondary footprints as reference (GT-free agreement).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import shapely

from geoqc.eval.common import (
    EvalInputError,
    EvalPaths,
    clean_polygons,
    load_eval_config,
    now_iso,
    pick,
    read_geojson,
    read_git_commit,
    read_json,
    rel,
    resolve_metric_crs,
)
from geoqc.eval.detection_metrics import (
    _assign_tiles,
    clip_to_tiles,
    evaluate_geodataframes,
    load_split_tiles,
)

SECONDARY_SOURCES: tuple[str, ...] = ("microsoft", "google")
COPY_IOU = 0.98  # a GT polygon this close to a footprint is an unedited pre-annotation draft


def count_copies(gt: gpd.GeoDataFrame, sec: gpd.GeoDataFrame, iou_min: float = COPY_IOU) -> int:
    """Number of GT polygons with IoU >= ``iou_min`` to some secondary footprint (same CRS)."""
    if not len(gt) or not len(sec):
        return 0
    g, f = gt.geometry.values, sec.geometry.values
    gi, si = shapely.STRtree(f).query(g, predicate="intersects")
    if not len(gi):
        return 0
    a, b = g[gi], f[si]
    union = shapely.area(shapely.union(a, b))
    iou = np.divide(
        shapely.area(shapely.intersection(a, b)), union, where=union > 0, out=np.zeros(len(gi))
    )
    return len(set(gi[iou >= iou_min].tolist()))


def load_secondary(
    paths: EvalPaths, secondary_path: Path | None = None, conn: Any = None
) -> tuple[gpd.GeoDataFrame | None, str | None, str | None]:
    """Return (footprints EPSG:4326, source name, where they came from)."""
    manifest = read_json(paths.manifest)
    source = pick(manifest, "secondary.source")
    source = str(source) if source and str(source).lower() != "null" else None
    path = Path(secondary_path) if secondary_path else paths.secondary
    if path.exists():
        gdf = read_geojson(path, "secondary footprints")
        if source is None and "source" in gdf.columns and len(gdf):
            source = str(gdf["source"].dropna().iloc[0])
        return gdf, source, rel(path, paths.repo_root)
    if conn is not None:
        sql = (
            "SELECT id, source, ST_AsBinary(geom) AS wkb FROM buildings_ref "
            "WHERE source IN ('microsoft', 'google')"
        )
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        if rows:
            from shapely import wkb

            gdf = gpd.GeoDataFrame(
                {"id": [r["id"] for r in rows], "source": [r["source"] for r in rows]},
                geometry=[wkb.loads(bytes(r["wkb"])) for r in rows],
                crs="EPSG:4326",
            )
            return gdf, source or str(gdf["source"].iloc[0]), "db:buildings_ref"
    return None, source, None


def compare(
    split: str,
    paths: EvalPaths,
    cfg: dict[str, Any],
    *,
    secondary_path: Path | None = None,
    labels_path: Path | None = None,
    detections_path: Path | None = None,
    allow_unverified: bool = False,
    conn: Any = None,
    method: str | None = None,
) -> dict[str, Any]:
    """Build the payload of ``reports/comparison.json`` for ``split`` (default: test)."""
    ecfg = load_eval_config(cfg)
    method = method or str(ecfg["match_method"])
    iou_thr = float(ecfg["iou_threshold"])
    min_area = float(ecfg["min_area_m2"])
    notes: list[str] = []

    tile_ids, strata, tiles4326 = load_split_tiles(paths, split)
    labels_path = Path(labels_path) if labels_path else paths.labels(split)
    detections_path = Path(detections_path) if detections_path else paths.detections

    gt4326 = read_geojson(labels_path, f"{split} labels") if labels_path.exists() else None
    if gt4326 is None:
        notes.append(f"labels not found: {rel(labels_path, paths.repo_root)}")
    det4326 = read_geojson(detections_path, "detections") if detections_path.exists() else None
    if det4326 is None:
        notes.append(f"detections not found: {rel(detections_path, paths.repo_root)}")
    sec4326, source, sec_from = load_secondary(paths, secondary_path, conn)
    if sec4326 is None:
        notes.append(
            "secondary footprints not found (data/interim/secondary_buildings.geojson or buildings_ref)"
        )

    sample = next((g for g in (gt4326, det4326, sec4326) if g is not None and len(g)), None)
    if sample is None:
        raise EvalInputError("nothing to compare: no labels, detections or secondary footprints")
    metric_crs, crs_source = resolve_metric_crs(cfg, sample, paths.manifest)
    tiles = None
    if tiles4326 is not None:
        tiles = tiles4326.to_crs(metric_crs)
        tiles["stratum"] = tiles["tile_id"].map(lambda t: strata.get(t, "unknown"))
    else:
        notes.append(
            "tile polygons unavailable: secondary footprints cannot be clipped to split tiles"
        )

    labels_verified = None
    gt = None
    if gt4326 is not None:
        gt = gt4326.to_crs(metric_crs)
        labels_verified = (
            bool(len(gt))
            and "verified" in gt.columns
            and bool(gt["verified"].fillna(False).astype(bool).all())
        )
        if not labels_verified and not allow_unverified:
            notes.append(
                "labels are not all verified: comparison against GT skipped (use --allow-unverified for a smoke run)"
            )
            gt = None
    det = det4326.to_crs(metric_crs) if det4326 is not None else None
    sec = clean_polygons(sec4326.to_crs(metric_crs)) if sec4326 is not None else None

    kwargs = dict(
        split=split,
        metric_crs=metric_crs,
        metric_crs_source=crs_source,
        iou_threshold=iou_thr,
        method=method,
        min_area_m2=min_area,
    )
    det_vs_gt = sec_vs_gt = det_vs_sec = None
    n_sec_in_tiles = None
    if gt is not None and det is not None:
        det_vs_gt = evaluate_geodataframes(gt, det, tiles, tile_ids, strata, **kwargs).summary()
    n_gt_copied = None
    if gt is not None and sec is not None:
        sec_vs_gt = evaluate_geodataframes(gt, sec, tiles, tile_ids, strata, **kwargs).summary()
        gt_poly = clean_polygons(gt)
        sec_cut = clip_to_tiles(sec, tiles, min_area) if tiles is not None else sec
        n_gt_copied = count_copies(gt_poly, sec_cut)
        if n_gt_copied:
            notes.append(
                f"{n_gt_copied} of {len(gt_poly)} GT polygons coincide with the secondary footprints "
                f"(IoU >= {COPY_IOU}, unedited drafts): secondary_vs_gt is biased upward"
            )
    if det is not None and sec is not None:
        if tiles is not None:
            sec_ref = clip_to_tiles(sec, tiles, min_area).reset_index(drop=True)
            sec_ref["tile_id"] = _assign_tiles(sec_ref, tiles, tile_ids)
            sec_ref = sec_ref[sec_ref["tile_id"].notna()]
            n_sec_in_tiles = int(len(sec_ref))
            det_vs_sec = evaluate_geodataframes(
                sec_ref, det, tiles, tile_ids, strata, **kwargs
            ).summary()
        else:
            notes.append("detection_vs_secondary needs tile polygons (data/tiles/tiles.gpkg)")

    return {
        "schema_version": "1.0",
        "generated_at": now_iso(),
        "git_commit": read_git_commit(paths.repo_root),
        "split": split,
        "is_metric": bool(labels_verified) if labels_verified is not None else False,
        "labels_verified": labels_verified,
        "labels_path": rel(labels_path, paths.repo_root) if gt4326 is not None else None,
        "detections_path": rel(detections_path, paths.repo_root) if det4326 is not None else None,
        "secondary_source": source,
        "secondary_path": sec_from,
        "n_secondary_total": int(len(sec)) if sec is not None else None,
        "n_secondary_in_tiles": n_sec_in_tiles,
        "n_gt_copied_from_secondary": n_gt_copied,
        "n_tiles": len(tile_ids),
        "iou_threshold": iou_thr,
        "match_method": method,
        "metric_crs": metric_crs,
        "detection_vs_gt": det_vs_gt,
        "secondary_vs_gt": sec_vs_gt,
        "detection_vs_secondary": det_vs_sec,
        "notes": notes,
    }
