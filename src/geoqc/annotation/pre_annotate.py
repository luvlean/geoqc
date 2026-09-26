"""Pre-annotation: draft building outlines for the tiles of a split (DECISIONS D-005).

Source polygons (Microsoft/Google footprints ``secondary_buildings.geojson`` or model
``detections.geojson``) are clipped to every tile of the split, promoted to MultiPolygon, filtered by
``min_area_m2`` and written to ``data/labels/<split>/draft.geojson`` with
``source=pre_annotation, verified=false``. OSM is deliberately not an allowed source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely

from geoqc.annotation.common import (
    STORAGE_CRS,
    Paths,
    check_split,
    metric_crs,
    read_tiles,
    read_tiles_split,
    read_vector,
    resolve_path,
    split_tiles,
    stable_json,
    to_multipolygon,
    write_geojson,
)

SOURCE_KEYS = ("secondary", "detections")


def clip_to_tiles(
    src: gpd.GeoDataFrame,
    tiles: gpd.GeoDataFrame,
    crs_metric: str,
    min_area_m2: float,
    simplify_m: float = 0.0,
) -> gpd.GeoDataFrame:
    """Clip polygons to tile geometries; one feature per (tile, source polygon) piece.

    Returns EPSG:4326 frame with ``tile_id`` and MultiPolygon geometry; pieces smaller than
    ``min_area_m2`` (slivers at tile borders) are dropped.
    """
    tiles_m = tiles.to_crs(crs_metric).reset_index(drop=True)
    src_m = src.to_crs(crs_metric).reset_index(drop=True)
    src_m = src_m[src_m.geometry.notna() & ~src_m.geometry.is_empty]
    src_m = src_m[src_m.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].reset_index(drop=True)
    records: list[dict[str, Any]] = []
    if len(src_m) > 0 and len(tiles_m) > 0:
        src_geoms = shapely.make_valid(src_m.geometry.values)
        t_idx, s_idx = shapely.STRtree(src_geoms).query(
            tiles_m.geometry.values, predicate="intersects"
        )
        for ti, si in zip(t_idx, s_idx, strict=True):
            piece = shapely.intersection(tiles_m.geometry.values[ti], src_geoms[si])
            piece = shapely.make_valid(piece)
            if simplify_m > 0:
                piece = shapely.simplify(piece, simplify_m, preserve_topology=True)
            mp = to_multipolygon(piece)
            if mp is None or mp.area < min_area_m2:
                continue
            records.append({"tile_id": str(tiles_m.loc[ti, "tile_id"]), "geometry": mp})
    if not records:
        return gpd.GeoDataFrame(
            {"tile_id": pd.Series(dtype=str)}, geometry=[], crs=crs_metric
        ).to_crs(STORAGE_CRS)
    out = gpd.GeoDataFrame(records, geometry="geometry", crs=crs_metric)
    return out.to_crs(STORAGE_CRS)


def with_label_properties(
    gdf: gpd.GeoDataFrame,
    split: str,
    source: str,
    verified: bool,
    annotator: str = "",
    notes: str = "",
) -> gpd.GeoDataFrame:
    """Attach the CONTRACTS §3.5 property set in canonical order."""
    out = gdf.copy()
    out["split"] = split
    out["source"] = source
    out["verified"] = bool(verified)
    out["annotator"] = annotator
    out["notes"] = notes
    cols = ["tile_id", "split", "source", "verified", "annotator", "notes", "geometry"]
    return gpd.GeoDataFrame(out[cols], geometry="geometry", crs=gdf.crs)


def resolve_source(
    cfg: dict[str, Any], source: str | None, source_path: Path | None
) -> tuple[str, Path]:
    """Pick the draft source: explicit path > explicit key > first existing key in priority order."""
    pcfg = cfg["annotation"].get("pre_annotation", {})
    if source_path is not None:
        if not Path(source_path).exists():
            raise FileNotFoundError(f"source file not found: {source_path}")
        return source or "custom", Path(source_path)
    if source is not None:
        if source not in SOURCE_KEYS:
            raise ValueError(
                f"--source must be one of {SOURCE_KEYS} (OSM is not allowed as a draft source)"
            )
        path = resolve_path(pcfg.get(f"{source}_path", f"data/interim/{source}_buildings.geojson"))
        if not path.exists():
            raise FileNotFoundError(f"{source} source not found: {path}")
        return source, path
    for key in pcfg.get("source_priority", list(SOURCE_KEYS)):
        path = resolve_path(pcfg.get(f"{key}_path", ""))
        if path.exists() and path.stat().st_size > 0:
            return key, path
    raise FileNotFoundError(
        "no pre-annotation source found (secondary_buildings.geojson / detections.geojson); "
        "run `geoqc data secondary` or `geoqc cv predict`, or pass --source-path"
    )


def pre_annotate(
    cfg: dict[str, Any],
    paths: Paths,
    split: str,
    source: str | None = None,
    source_path: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build ``draft.geojson`` for ``split``; refuses to overwrite an existing draft unless ``force``."""
    check_split(split)
    acfg = cfg["annotation"]
    out_path = paths.split_dir(split) / "draft.geojson"
    if out_path.exists() and not force:
        raise FileExistsError(f"{out_path} exists; pass --force to rebuild the draft")
    source_key, src_path = resolve_source(cfg, source, source_path)
    tiles = read_tiles(paths.tiles_index)
    tiles_split_df = read_tiles_split(paths.tiles_split_csv)
    tiles_s = split_tiles(tiles, tiles_split_df, split)
    src = read_vector(src_path)
    crs_m = metric_crs(cfg, tiles.geometry, paths.manifest)
    pieces = clip_to_tiles(
        src,
        tiles_s,
        crs_m,
        float(acfg.get("min_area_m2", 12.0)),
        float(acfg.get("pre_annotation", {}).get("simplify_tolerance_m", 0.0)),
    )
    draft = with_label_properties(pieces, split, "pre_annotation", verified=False)
    write_geojson(draft, out_path)

    per_tile = draft.groupby("tile_id").size().to_dict() if len(draft) else {}
    summary = {
        "split": split,
        "source": source_key,
        "source_path": str(src_path),
        "n_source_features": int(len(src)),
        "n_tiles": int(len(tiles_s)),
        "n_draft_features": int(len(draft)),
        "tiles_without_draft": sorted(set(tiles_s["tile_id"]) - set(per_tile)),
        "per_tile": {k: int(v) for k, v in sorted(per_tile.items())},
        "metric_crs": crs_m,
        "output": str(out_path),
    }
    (paths.split_dir(split) / "draft_summary.json").write_text(
        stable_json(summary), encoding="utf-8"
    )
    return summary
