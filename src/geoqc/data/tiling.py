"""Tile the AOI COG into fixed-size GeoTIFFs with overlap and build ``tiles.gpkg`` (contract §3.3)."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.windows import Window
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

log = logging.getLogger(__name__)


def grid_positions(length_px: int, size_px: int, overlap_px: int) -> list[int]:
    """Start offsets along one axis so that windows of ``size_px`` with ``overlap_px`` cover ``length_px``.

    The last window is padded (not shifted back), so every start is a multiple of the step and
    ``(row, col)`` map to a deterministic position: ``start = index * (size - overlap)``.
    """
    if length_px <= 0:
        return []
    step = size_px - overlap_px
    if step <= 0:
        raise ValueError("overlap must be smaller than tile size")
    if length_px <= size_px:
        return [0]
    n = math.ceil((length_px - overlap_px) / step)
    return [i * step for i in range(n)]


def tile_raster(
    cog_path: Path,
    out_dir: Path,
    index_path: Path,
    *,
    size_px: int = 512,
    overlap_px: int = 64,
    nodata: int = 0,
    aoi_geom: BaseGeometry | None = None,
    layer: str = "tiles",
    name_pattern: str = "{row}_{col}.tif",
    force: bool = False,
) -> dict[str, Any]:
    """Write ``{row}_{col}.tif`` tiles and the GPKG index; return counts and coverage info.

    ``aoi_geom`` (in the COG CRS) restricts the output to tiles intersecting the AOI; when it is
    None every grid cell is written. Tiles at the right/bottom edge are padded with ``nodata``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    n_written = 0
    n_skipped_outside = 0
    n_skipped_empty = 0
    with rasterio.open(cog_path) as src:
        rows = grid_positions(src.height, size_px, overlap_px)
        cols = grid_positions(src.width, size_px, overlap_px)
        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            width=size_px,
            height=size_px,
            count=3,
            dtype="uint8",
            nodata=nodata,
            tiled=False,
            compress="deflate",
            predictor=2,
            blockxsize=None,
            blockysize=None,
        )
        for k in ("blockxsize", "blockysize"):
            profile.pop(k, None)
        for r_idx, row_off in enumerate(rows):
            for c_idx, col_off in enumerate(cols):
                tile_id = f"{r_idx}_{c_idx}"
                win = Window(col_off, row_off, size_px, size_px)
                transform = src.window_transform(win)
                bounds = rasterio.windows.bounds(win, src.transform)
                geom_native = box(*bounds)
                if aoi_geom is not None and not aoi_geom.intersects(geom_native):
                    n_skipped_outside += 1
                    continue
                path = out_dir / name_pattern.format(row=r_idx, col=c_idx)
                if path.exists() and not force:
                    data_exists = True
                else:
                    data_exists = False
                    data = src.read(
                        indexes=[1, 2, 3],
                        window=win,
                        boundless=True,
                        fill_value=nodata,
                    ).astype("uint8")
                    if not np.any(data):
                        n_skipped_empty += 1
                        continue
                    profile.update(transform=transform)
                    with rasterio.open(path, "w", **profile) as dst:
                        dst.write(data)
                if not data_exists:
                    n_written += 1
                records.append(
                    {
                        "tile_id": tile_id,
                        "row": r_idx,
                        "col": c_idx,
                        "path": str(path.relative_to(out_dir.parent.parent))
                        if out_dir.is_relative_to(out_dir.parent.parent)
                        else str(path),
                        "geometry": geom_native,
                    }
                )
        crs = src.crs
    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs).to_crs(4326)
    gdf = gdf[["tile_id", "row", "col", "path", "geometry"]]
    if index_path.exists():
        index_path.unlink()
    gdf.to_file(index_path, layer=layer, driver="GPKG")
    coverage = None
    if aoi_geom is not None and len(records) > 0:
        union = gpd.GeoSeries([r["geometry"] for r in records], crs=crs).union_all()
        coverage = (
            float(aoi_geom.intersection(union).area / aoi_geom.area) if aoi_geom.area > 0 else None
        )
    result = {
        "n_tiles": len(records),
        "n_written": n_written,
        "n_skipped_outside_aoi": n_skipped_outside,
        "n_skipped_empty": n_skipped_empty,
        "n_rows": len(rows),
        "n_cols": len(cols),
        "aoi_coverage": coverage,
        "index": str(index_path),
    }
    log.info("tiling: %s", result)
    return result


def aoi_to_crs(aoi_wgs84: BaseGeometry, dst_crs: Any) -> BaseGeometry:
    """Project an EPSG:4326 geometry into ``dst_crs`` (anything pyproj understands)."""
    import pyproj

    tr = pyproj.Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True).transform
    return shp_transform(tr, aoi_wgs84)
