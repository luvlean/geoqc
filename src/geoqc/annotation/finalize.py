"""Finalize: turn the QGIS working files into the contract files of a split.

Reads ``data/labels/<split>/work.gpkg`` (layers ``buildings`` and ``recheck``) — or, when absent,
``buildings.geojson`` / ``recheck.geojson`` in place — and writes the CONTRACTS §3.5 files:
geometries are made valid, promoted to MultiPolygon, assigned to the tile with the largest overlap,
clipped to that tile, slivers below ``min_area_m2`` are dropped and the property set is normalised
(``verified`` -> bool, missing ``source`` -> ``manual``, ``annotator``/``notes`` -> strings).
The previous ``buildings.geojson`` is kept as ``buildings.geojson.bak``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely

from geoqc.annotation.common import (
    LABEL_PROPERTIES,
    SOURCES,
    STORAGE_CRS,
    Paths,
    as_bool,
    check_split,
    metric_crs,
    read_tiles,
    read_tiles_split,
    read_vector,
    split_tiles,
    stable_json,
    to_multipolygon,
    write_geojson,
)
from geoqc.annotation.validate import assign_tile_ids


def normalise_labels(
    gdf: gpd.GeoDataFrame,
    tiles: gpd.GeoDataFrame,
    split: str,
    crs_metric: str,
    min_area_m2: float,
    annotator: str | None = None,
    default_source: str = "manual",
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Clean one labels frame; returns (frame in EPSG:4326, stats)."""
    stats = {
        "n_input": int(len(gdf)),
        "dropped_empty": 0,
        "dropped_small": 0,
        "clipped": 0,
        "tile_reassigned": 0,
    }
    if len(gdf) == 0:
        empty = gpd.GeoDataFrame(
            {c: pd.Series(dtype=object) for c in LABEL_PROPERTIES}, geometry=[], crs=STORAGE_CRS
        )
        empty["verified"] = empty["verified"].astype(bool)
        return empty, stats
    work = gdf.to_crs(crs_metric).reset_index(drop=True)
    keep = work.geometry.notna() & ~work.geometry.is_empty
    stats["dropped_empty"] = int((~keep).sum())
    work = work[keep].reset_index(drop=True)
    work["geometry"] = [to_multipolygon(shapely.make_valid(g)) for g in work.geometry.values]
    keep = work.geometry.notna()
    stats["dropped_empty"] += int((~keep).sum())
    work = work[keep].reset_index(drop=True)

    tiles_m = tiles.to_crs(crs_metric)
    before = (
        work["tile_id"].fillna("").astype(str)
        if "tile_id" in work.columns
        else pd.Series([""] * len(work))
    )
    work = assign_tile_ids(work, tiles_m, crs_metric)
    stats["tile_reassigned"] = int((before.to_numpy() != work["tile_id"].to_numpy()).sum())
    tile_geom = tiles_m.set_index("tile_id")["geometry"]

    geoms = []
    clipped = 0
    for tid, g in zip(work["tile_id"], work.geometry.values, strict=True):
        if tid in tile_geom.index:
            tg = tile_geom.loc[tid]
            if not tg.contains(g):
                g = to_multipolygon(shapely.make_valid(g.intersection(tg)))
                clipped += 1
        geoms.append(g)
    stats["clipped"] = clipped
    work["geometry"] = geoms
    keep = work.geometry.notna()
    work = work[keep].reset_index(drop=True)
    areas = work.geometry.area
    stats["dropped_small"] = int((areas < min_area_m2).sum())
    work = work[areas >= min_area_m2].reset_index(drop=True)

    out = work.copy()
    out["split"] = split
    src = out["source"] if "source" in out.columns else pd.Series([None] * len(out))
    out["source"] = [s if s in SOURCES else default_source for s in src]
    ver = out["verified"] if "verified" in out.columns else pd.Series([None] * len(out))
    out["verified"] = [bool(as_bool(v)) if as_bool(v) is not None else False for v in ver]
    ann = out["annotator"] if "annotator" in out.columns else pd.Series([None] * len(out))
    out["annotator"] = [
        str(a) if isinstance(a, str) and a.strip() else (annotator or "") for a in ann
    ]
    notes = out["notes"] if "notes" in out.columns else pd.Series([None] * len(out))
    out["notes"] = [str(n) if isinstance(n, str) else "" for n in notes]
    out = gpd.GeoDataFrame(
        out[[*LABEL_PROPERTIES, "geometry"]], geometry="geometry", crs=crs_metric
    )
    stats["n_output"] = int(len(out))
    stats["n_verified"] = int(out["verified"].sum())
    return out.to_crs(STORAGE_CRS), stats


def _read_layer(path: Path, layer: str | None) -> gpd.GeoDataFrame | None:
    if not path.exists():
        return None
    if layer is not None:
        layers = {name for name, _ in pyogrio.list_layers(path)}
        if layer not in layers:
            return None
        gdf = gpd.read_file(path, layer=layer)
    else:
        gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(STORAGE_CRS)
    return gdf.to_crs(STORAGE_CRS)


def finalize_split(
    cfg: dict[str, Any],
    paths: Paths,
    split: str,
    annotator: str | None = None,
    input_path: Path | None = None,
) -> dict[str, Any]:
    """Write ``buildings.geojson`` (+ ``recheck.geojson``) for ``split`` from the working files."""
    check_split(split)
    acfg = cfg["annotation"]
    min_area = float(acfg.get("min_area_m2", 12.0))
    sdir = paths.split_dir(split)
    tiles_all = read_tiles(paths.tiles_index)
    tiles_s = split_tiles(tiles_all, read_tiles_split(paths.tiles_split_csv), split)
    crs_m = metric_crs(cfg, tiles_all.geometry, paths.manifest)

    work_gpkg = Path(input_path) if input_path else sdir / "work.gpkg"
    summary: dict[str, Any] = {"split": split, "metric_crs": crs_m, "layers": {}}
    if work_gpkg.exists() and work_gpkg.suffix.lower() == ".gpkg":
        summary["input"] = str(work_gpkg)
        buildings = _read_layer(work_gpkg, "buildings")
        recheck = _read_layer(work_gpkg, "recheck")
        if buildings is None:
            raise ValueError(f"{work_gpkg} has no layer 'buildings'")
    else:
        b_path = Path(input_path) if input_path else sdir / "buildings.geojson"
        if not b_path.exists():
            raise FileNotFoundError(f"neither {sdir / 'work.gpkg'} nor {b_path} exists")
        summary["input"] = str(b_path)
        buildings = read_vector(b_path)
        recheck = _read_layer(sdir / "recheck.geojson", None)

    out_b = sdir / "buildings.geojson"
    if out_b.exists():
        shutil.copyfile(out_b, out_b.with_suffix(".geojson.bak"))
    clean_b, st_b = normalise_labels(buildings, tiles_s, split, crs_m, min_area, annotator)
    write_geojson(clean_b, out_b)
    summary["layers"]["buildings"] = {**st_b, "output": str(out_b)}

    if recheck is not None and len(recheck) > 0:
        out_r = sdir / "recheck.geojson"
        clean_r, st_r = normalise_labels(recheck, tiles_s, split, crs_m, min_area, annotator)
        write_geojson(clean_r, out_r)
        summary["layers"]["recheck"] = {**st_r, "output": str(out_r)}
    else:
        summary["layers"]["recheck"] = {"n_input": 0, "output": None}

    (sdir / "finalize_summary.json").write_text(stable_json(summary), encoding="utf-8")
    return summary
