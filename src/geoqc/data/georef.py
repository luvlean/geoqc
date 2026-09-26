"""Georeference check (quality gate G1): offset between OSM footprints and the image.

Method
------
1. Pick control buildings from OSM: mid-sized (``min_area_m2``..``max_area_m2``), compact
   (area / minimum rotated rectangle >= ``min_rectangularity``), isolated (no other building
   within ``isolation_m``), low-rise when ``building:levels`` is tagged (roof lean), fully inside
   the AOI and on valid pixels.
2. For each control: cut an image window around the footprint padded by ``search_radius_m``;
   compute a gradient-magnitude image (Sobel on grey) and rasterize the OSM outline (1 px, dilated).
   Cross-correlate the two with FFT; the argmax within +-search radius is the image-vs-vector
   offset for that building. Controls with a weak peak (peak / mean < ``min_peak_ratio``) are dropped.
3. ``median_offset_m`` = median of per-building offset lengths (G1). ``systematic_shift_m`` =
   (median dx, median dy) — the translation that ``preprocess --shift`` applies when the gate fails.

Limits: roofs are not footprints (tall buildings lean off-nadir, so only low-rise controls are
used), trees/shadows add spurious edges, and a pure translation cannot fix rotation/scale errors.
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio import features
from rasterio.windows import Window, from_bounds
from scipy import ndimage
from scipy.signal import fftconvolve
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)


def select_controls(
    buildings: gpd.GeoDataFrame,
    aoi_metric: BaseGeometry,
    cfg: dict[str, Any],
) -> gpd.GeoDataFrame:
    """Filter OSM buildings (already in the metric CRS) down to good control candidates."""
    g = buildings.copy()
    g = g[g.geometry.notna() & g.geometry.is_valid & ~g.geometry.is_empty]
    g = g[g.geometry.geom_type == "Polygon"]
    g["area_m2"] = g.geometry.area
    g = g[(g["area_m2"] >= cfg["min_area_m2"]) & (g["area_m2"] <= cfg["max_area_m2"])]
    rect_area = g.geometry.apply(lambda p: p.minimum_rotated_rectangle.area)
    g["rectangularity"] = g["area_m2"] / rect_area.replace(0, np.nan)
    g = g[g["rectangularity"] >= cfg["min_rectangularity"]]
    # Fully inside the AOI shrunk by the search radius so windows never leave the image.
    inner = aoi_metric.buffer(-(cfg["search_radius_m"] + 5.0))
    g = g[g.geometry.within(inner)]

    def levels_ok(tags: Any) -> bool:
        if not isinstance(tags, dict):
            return True
        lv = tags.get("building:levels")
        if lv is None:
            return True
        try:
            return float(str(lv).split(";")[0].replace(",", ".")) <= cfg["max_levels"]
        except ValueError:
            return True

    if "tags" in g.columns:
        g = g[g["tags"].apply(levels_ok)]
    # Isolation: no *other* building within isolation_m.
    all_sindex = buildings.sindex
    keep = []
    for idx, row in g.iterrows():
        probe = row.geometry.buffer(cfg["isolation_m"])
        hits = list(all_sindex.query(probe, predicate="intersects"))
        others = [h for h in hits if buildings.index[h] != idx]
        keep.append(len(others) == 0)
    g = g[np.array(keep, dtype=bool)] if len(g) else g
    return g


def _gradient_magnitude(rgb: np.ndarray) -> np.ndarray:
    grey = rgb.astype(np.float32).mean(axis=0)
    gx = ndimage.sobel(grey, axis=1)
    gy = ndimage.sobel(grey, axis=0)
    mag = np.hypot(gx, gy)
    if mag.max() > 0:
        mag /= mag.max()
    return mag


def _outline_mask(poly: Polygon, transform: rasterio.Affine, shape: tuple[int, int]) -> np.ndarray:
    filled = features.rasterize(
        [(poly, 1)], out_shape=shape, transform=transform, fill=0, dtype="uint8"
    )
    eroded = ndimage.binary_erosion(filled.astype(bool), iterations=1)
    outline = filled.astype(bool) & ~eroded
    return ndimage.binary_dilation(outline, iterations=1).astype(np.float32)


def offset_for_building(
    ds: rasterio.io.DatasetReader,
    poly: Polygon,
    *,
    search_radius_m: float,
    nodata: int = 0,
) -> dict[str, Any] | None:
    """Return ``{dx_m, dy_m, dist_m, peak_ratio}`` for one control polygon or None when unusable."""
    res = float(abs(ds.res[0]))
    r_px = int(math.ceil(search_radius_m / res))
    minx, miny, maxx, maxy = poly.bounds
    pad = search_radius_m * 2.0
    win = from_bounds(minx - pad, miny - pad, maxx + pad, maxy + pad, transform=ds.transform)
    win = win.round_offsets().round_lengths()
    full = Window(0, 0, ds.width, ds.height)
    win = win.intersection(full)
    if win.width < 4 * r_px or win.height < 4 * r_px:
        return None
    data = ds.read(indexes=[1, 2, 3], window=win)
    valid = ~np.all(data == nodata, axis=0)
    if valid.mean() < 0.98:
        return None
    win_tr = ds.window_transform(win)
    shape = (int(win.height), int(win.width))
    grad = _gradient_magnitude(data)
    grad[~valid] = 0.0
    outline = _outline_mask(poly, win_tr, shape)
    if outline.sum() < 8:
        return None
    # Cross-correlation: corr[k] = sum_x grad[x + k] * outline[x]  (mode 'same').
    corr = fftconvolve(grad, outline[::-1, ::-1], mode="same")
    cy, cx = shape[0] // 2, shape[1] // 2
    y0, y1 = max(cy - r_px, 0), min(cy + r_px + 1, shape[0])
    x0, x1 = max(cx - r_px, 0), min(cx + r_px + 1, shape[1])
    sub = corr[y0:y1, x0:x1]
    if sub.size == 0 or not np.isfinite(sub).all():
        return None
    py, px = np.unravel_index(int(np.argmax(sub)), sub.shape)
    mean = float(sub.mean()) if sub.mean() > 0 else 1e-9
    peak_ratio = float(sub[py, px] / mean)

    # Sub-pixel refinement by a parabola through the three neighbours.
    def refine(v: np.ndarray, i: int) -> float:
        if 0 < i < len(v) - 1:
            a, b, c = v[i - 1], v[i], v[i + 1]
            den = a - 2 * b + c
            if den != 0:
                return float(i + 0.5 * (a - c) / den)
        return float(i)

    fy = refine(sub[:, px], py)
    fx = refine(sub[py, :], px)
    # Shift of the image content relative to the vector, in pixels: (fx - cx0, fy - cy0),
    # where the zero-shift index is at the centre of the search sub-window.
    dx_px = fx - (cx - x0)
    dy_px = fy - (cy - y0)
    # Because `mode='same'` centres the kernel with an offset for even sizes, correct by parity.
    dx_px -= 0.5 * ((shape[1] + 1) % 2)
    dy_px -= 0.5 * ((shape[0] + 1) % 2)
    dx_m = dx_px * res
    dy_m = -dy_px * res  # image rows grow downwards, northing grows upwards
    return {
        "dx_m": round(float(dx_m), 3),
        "dy_m": round(float(dy_m), 3),
        "dist_m": round(float(math.hypot(dx_m, dy_m)), 3),
        "peak_ratio": round(peak_ratio, 3),
    }


def check_georef(
    cog_path: Path,
    osm_geojson: Path,
    aoi_wgs84: BaseGeometry,
    cfg: dict[str, Any],
    *,
    report_path: Path,
    metric_epsg: int,
) -> dict[str, Any]:
    """Estimate the image-vs-OSM offset on control buildings and write ``reports/georef.json``."""
    rng = random.Random(cfg.get("random_seed", 42))
    gdf = gpd.read_file(osm_geojson)
    if "tags" in gdf.columns and len(gdf) and isinstance(gdf["tags"].iloc[0], str):
        gdf["tags"] = gdf["tags"].apply(lambda s: json.loads(s) if isinstance(s, str) else s)
    gdf = gdf.set_crs(4326, allow_override=True).to_crs(metric_epsg)
    aoi_metric = gpd.GeoSeries([aoi_wgs84], crs=4326).to_crs(metric_epsg).iloc[0]
    controls = select_controls(gdf, aoi_metric, cfg)
    ids = list(controls.index)
    rng.shuffle(ids)
    per_building: list[dict[str, Any]] = []
    n_weak = 0
    n_unusable = 0
    with rasterio.open(cog_path) as ds:
        nodata = int(ds.nodata) if ds.nodata is not None else 0
        for idx in ids:
            if len(per_building) >= cfg["n_control_target"]:
                break
            row = controls.loc[idx]
            res = offset_for_building(
                ds, row.geometry, search_radius_m=cfg["search_radius_m"], nodata=nodata
            )
            if res is None:
                n_unusable += 1
                continue
            if res["peak_ratio"] < cfg["min_peak_ratio"]:
                n_weak += 1
                continue
            res["osm_id"] = (
                int(row["osm_id"]) if "osm_id" in row and row["osm_id"] is not None else None
            )
            res["area_m2"] = round(float(row["area_m2"]), 1)
            c = row.geometry.centroid
            res["centroid_utm"] = [round(c.x, 2), round(c.y, 2)]
            per_building.append(res)
    n = len(per_building)
    if n:
        dists = np.array([b["dist_m"] for b in per_building])
        dxs = np.array([b["dx_m"] for b in per_building])
        dys = np.array([b["dy_m"] for b in per_building])
        median_offset = float(np.median(dists))
        sys_dx, sys_dy = float(np.median(dxs)), float(np.median(dys))
        # Robust spread: median absolute deviation of the offset vectors around the median vector.
        resid = np.hypot(dxs - sys_dx, dys - sys_dy)
        mad = float(np.median(resid))
        p90 = float(np.percentile(dists, 90))
    else:
        median_offset = sys_dx = sys_dy = mad = p90 = float("nan")
    systematic_len = math.hypot(sys_dx, sys_dy) if n else float("nan")
    recommend_shift = bool(
        n >= cfg["n_control_min"]
        and median_offset > cfg["apply_shift_over_m"]
        and systematic_len >= cfg["min_systematic_shift_m"]
    )
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cog": str(cog_path),
        "osm": str(osm_geojson),
        "metric_epsg": metric_epsg,
        "method": "FFT cross-correlation of Sobel gradient magnitude with the rasterized OSM outline, per control building; median of offset lengths",
        "params": {k: cfg[k] for k in cfg if k != "output"},
        "n_candidates": int(len(controls)),
        "n_control_points": n,
        "n_rejected_weak_peak": n_weak,
        "n_unusable_window": n_unusable,
        "median_offset_m": round(median_offset, 3) if n else None,
        "p90_offset_m": round(p90, 3) if n else None,
        "systematic_shift_m": [round(sys_dx, 3), round(sys_dy, 3)] if n else None,
        "systematic_shift_len_m": round(systematic_len, 3) if n else None,
        "mad_around_systematic_m": round(mad, 3) if n else None,
        "gate_G1": {
            "target_m": cfg["apply_shift_over_m"],
            "passed": bool(
                n >= cfg["n_control_min"] and median_offset <= cfg["apply_shift_over_m"]
            ),
        },
        "recommend_shift": recommend_shift,
        "enough_controls": bool(n >= cfg["n_control_min"]),
        "controls": per_building,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    return report
