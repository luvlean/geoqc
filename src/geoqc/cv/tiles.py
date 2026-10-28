"""Tile index and raster access shared by predict, export-dataset, threshold-sweep and bundle.

Contract (docs/CONTRACTS.md §3.3): ``data/tiles/{row}_{col}.tif`` 512x512 px with a 64 px
overlap, nodata = 0, all tiles in the metric UTM CRS of the COG; index ``tiles.gpkg`` layer
``tiles`` with ``tile_id, row, col, path, geom`` (EPSG:4326). When the index is missing the
index is rebuilt from the ``*.tif`` files themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine
from shapely.geometry import box

from geoqc.config import REPO_ROOT, resolve_path

TILE_NAME_RE = re.compile(r"^(?P<row>\d+)_(?P<col>\d+)\.tif$")
SPLITS = ("all", "train", "val", "test")


@dataclass
class TileRaster:
    """One tile loaded in memory: RGB uint8 image plus georeferencing."""

    tile_id: str
    path: Path
    image: np.ndarray  # H x W x 3, uint8, RGB
    transform: Affine
    crs: CRS
    nodata_mask: np.ndarray  # H x W bool, True where every band == 0

    @property
    def shape(self) -> tuple[int, int]:
        return self.image.shape[0], self.image.shape[1]


def utm_epsg_for(lon: float, lat: float) -> int:
    """UTM EPSG code for a lon/lat; delegates to geoqc.data.crs when the Data module exists."""
    try:
        from geoqc.data.crs import utm_epsg_for as _impl

        return int(_impl(lon, lat))
    except Exception:  # Data module not available yet: local formula
        zone = int((lon + 180.0) // 6.0) + 1
        zone = min(max(zone, 1), 60)
        return (32600 if lat >= 0 else 32700) + zone


def _index_from_files(tiles_dir: Path) -> gpd.GeoDataFrame:
    rows: list[dict] = []
    for path in sorted(tiles_dir.glob("*.tif")):
        m = TILE_NAME_RE.match(path.name)
        if not m:
            continue
        with rasterio.open(path) as src:
            b = src.bounds
            geom = box(b.left, b.bottom, b.right, b.top)
            crs = src.crs
        rows.append(
            {
                "tile_id": f"{int(m['row'])}_{int(m['col'])}",
                "row": int(m["row"]),
                "col": int(m["col"]),
                "path": str(path),
                "geometry": geom,
            }
        )
    if not rows:
        return gpd.GeoDataFrame(
            {"tile_id": [], "row": [], "col": [], "path": []},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
        )
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)
    return gdf.to_crs("EPSG:4326") if gdf.crs is not None else gdf


def load_tile_index(
    tiles_dir: str | Path | None = None, index_path: str | Path | None = None
) -> gpd.GeoDataFrame:
    """Return the tile index (EPSG:4326) with absolute ``path`` values.

    Prefers ``tiles.gpkg`` (layer ``tiles``); falls back to scanning ``*.tif``.
    """
    tdir = resolve_path(tiles_dir or "data/tiles")
    ipath = resolve_path(index_path) if index_path else tdir / "tiles.gpkg"
    if ipath.exists():
        gdf = gpd.read_file(ipath, layer="tiles")
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
    else:
        gdf = _index_from_files(tdir)
    if len(gdf) == 0:
        return gdf

    def _abs(p: str) -> str:
        pp = Path(p)
        if pp.is_absolute():
            return str(pp)
        cand = tdir / pp.name
        return str(cand if cand.exists() else REPO_ROOT / pp)

    gdf = gdf.copy()
    gdf["path"] = gdf["path"].astype(str).map(_abs)
    gdf["tile_id"] = gdf["tile_id"].astype(str)
    return gdf.sort_values("tile_id").reset_index(drop=True)


def read_split_csv(split_csv: str | Path | None = None) -> pd.DataFrame:
    """Read ``data/labels/tiles_split.csv`` (tile_id, split, stratum, row, col)."""
    path = resolve_path(split_csv or "data/labels/tiles_split.csv")
    df = pd.read_csv(path, dtype={"tile_id": str, "split": str})
    if "tile_id" not in df.columns or "split" not in df.columns:
        raise ValueError(f"{path} must have columns tile_id and split")
    return df


def filter_by_split(
    index: gpd.GeoDataFrame, split: str, split_csv: str | Path | None = None
) -> gpd.GeoDataFrame:
    """Restrict the index to tiles of ``split`` (``all`` returns the index untouched)."""
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}, got {split!r}")
    if split == "all":
        return index
    df = read_split_csv(split_csv)
    keep = set(df.loc[df["split"] == split, "tile_id"].astype(str))
    return index[index["tile_id"].isin(keep)].reset_index(drop=True)


def read_tile(path: str | Path, tile_id: str | None = None) -> TileRaster:
    """Read a tile as RGB uint8 (1 band is replicated, extra bands dropped)."""
    p = Path(path)
    with rasterio.open(p) as src:
        count = src.count
        bands = [1, 2, 3] if count >= 3 else [1] * 3
        arr = src.read(bands)  # (3, H, W)
        transform = src.transform
        crs = src.crs
    if arr.dtype != np.uint8:
        finite = arr[np.isfinite(arr)]
        hi = float(finite.max()) if finite.size else 1.0
        scale = 255.0 / hi if hi > 255 else 1.0
        arr = np.clip(np.nan_to_num(arr) * scale, 0, 255).astype(np.uint8)
    image = np.ascontiguousarray(np.transpose(arr, (1, 2, 0)))
    nodata_mask = ~(image != 0).any(axis=2)
    if tile_id is None:
        m = TILE_NAME_RE.match(p.name)
        tile_id = f"{int(m['row'])}_{int(m['col'])}" if m else p.stem
    return TileRaster(
        tile_id=tile_id,
        path=p,
        image=image,
        transform=transform,
        crs=crs,
        nodata_mask=nodata_mask,
    )


def metric_crs_for(crs: CRS | None, index: gpd.GeoDataFrame | None = None) -> CRS:
    """Metric CRS for area/IoU work: the tile CRS when projected, else the AOI UTM zone."""
    if crs is not None and crs.is_projected:
        return crs
    if index is not None and len(index):
        c = index.geometry.union_all().centroid
        return CRS.from_epsg(utm_epsg_for(c.x, c.y))
    raise ValueError("cannot derive a metric CRS: tiles are geographic and index is empty")
