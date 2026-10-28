"""Shared helpers for the annotation module: config access, paths, CRS, I/O.

Everything here is pure Python / GeoPandas and safe to import in tests. Paths default to the
repository layout from ``config/pipeline.yaml`` but every public function accepts explicit
paths so the module can run on synthetic data in a temporary directory.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from geoqc.config import CONFIG_DIR, load_config, repo_relative, resolve_path

STORAGE_CRS = "EPSG:4326"
SPLITS = ("train", "val", "test")  # D-021: train labels fine-tune, val tunes, test is final
STRATA = ("dense_multistorey", "low_rise_private", "industrial", "mixed")
LABEL_PROPERTIES = ("tile_id", "split", "source", "verified", "annotator", "notes")
SOURCES = ("manual", "pre_annotation")
TILES_SPLIT_COLUMNS = ["tile_id", "split", "stratum", "row", "col"]


def annotation_config(config_dir: Path | None = None) -> dict[str, Any]:
    """Return the merged config (pipeline + annotation + gates)."""
    cfg = load_config(config_dir)
    cfg.setdefault("annotation", {})
    return cfg


@dataclass(frozen=True)
class Paths:
    """Resolved paths used by the module (all absolute)."""

    labels_dir: Path
    tiles_index: Path
    interim_dir: Path
    reports_dir: Path
    aoi_geojson: Path
    manifest: Path

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any],
        labels_dir: Path | None = None,
        tiles_index: Path | None = None,
        config_dir: Path | None = None,
    ) -> Paths:
        paths = cfg.get("paths", {})
        tiles = cfg.get("tiles", {})
        cfg_dir = Path(config_dir or os.environ.get("GEOQC_CONFIG_DIR", CONFIG_DIR))
        return cls(
            labels_dir=Path(labels_dir).resolve()
            if labels_dir
            else resolve_path(paths.get("labels", "data/labels")),
            tiles_index=Path(tiles_index).resolve()
            if tiles_index
            else resolve_path(tiles.get("index", "data/tiles/tiles.gpkg")),
            interim_dir=resolve_path(paths.get("interim", "data/interim")),
            reports_dir=resolve_path(paths.get("reports", "reports")),
            aoi_geojson=cfg_dir / "aoi.geojson",
            manifest=resolve_path(paths.get("data_dir", "data")) / "manifest.json",
        )

    def split_dir(self, split: str) -> Path:
        return self.labels_dir / split

    @property
    def tiles_split_csv(self) -> Path:
        return self.labels_dir / "tiles_split.csv"

    @property
    def recheck_tiles_csv(self) -> Path:
        return self.labels_dir / "recheck_tiles.csv"


def check_split(split: str) -> str:
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}, got {split!r}")
    return split


# --------------------------------------------------------------------------- CRS helpers


def utm_epsg_for(lon: float, lat: float) -> int:
    """UTM EPSG code for a WGS84 point (32601–32660 north, 32701–32760 south)."""
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def metric_crs(
    cfg: dict[str, Any], geoms: gpd.GeoSeries | None = None, manifest: Path | None = None
) -> str:
    """Resolve the metric CRS: pipeline.yaml -> manifest.json -> UTM zone of the data centroid."""
    metric = (cfg.get("crs") or {}).get("metric")
    if metric:
        return str(metric)
    if manifest is not None and manifest.exists():
        try:
            epsg = json.loads(manifest.read_text(encoding="utf-8")).get("aoi", {}).get("utm_epsg")
            if epsg:
                return f"EPSG:{int(epsg)}"
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    if geoms is not None and len(geoms) > 0:
        wgs = geoms.to_crs(STORAGE_CRS) if geoms.crs is not None else geoms
        c = wgs.union_all().centroid
        return f"EPSG:{utm_epsg_for(c.x, c.y)}"
    raise ValueError("cannot resolve metric CRS: set crs.metric in config/pipeline.yaml")


# --------------------------------------------------------------------------- I/O helpers


def read_tiles(tiles_index: Path, layer: str = "tiles") -> gpd.GeoDataFrame:
    """Read ``tiles.gpkg`` (CONTRACTS §3.3) and normalise columns."""
    if not tiles_index.exists():
        raise FileNotFoundError(f"tiles index not found: {tiles_index}")
    tiles = gpd.read_file(tiles_index, layer=layer)
    missing = {"tile_id", "row", "col"} - set(tiles.columns)
    if missing:
        raise ValueError(f"tiles index lacks columns {sorted(missing)}")
    tiles["tile_id"] = tiles["tile_id"].astype(str)
    tiles["row"] = tiles["row"].astype(int)
    tiles["col"] = tiles["col"].astype(int)
    if tiles.crs is None:
        tiles = tiles.set_crs(STORAGE_CRS)
    return tiles.to_crs(STORAGE_CRS)


def read_tiles_split(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} not found: run `geoqc annotation select-tiles` first")
    df = pd.read_csv(csv_path, dtype={"tile_id": str, "split": str, "stratum": str})
    missing = set(TILES_SPLIT_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{csv_path} lacks columns {sorted(missing)}")
    return df


def split_tiles(tiles: gpd.GeoDataFrame, tiles_split: pd.DataFrame, split: str) -> gpd.GeoDataFrame:
    """Tiles (geometry + stratum) that belong to ``split``."""
    sel = tiles_split[tiles_split["split"] == check_split(split)]
    out = tiles.merge(sel[["tile_id", "stratum"]], on="tile_id", how="inner")
    if out.empty:
        raise ValueError(f"no tiles for split={split!r} in tiles_split.csv")
    return gpd.GeoDataFrame(out, geometry="geometry", crs=tiles.crs)


def read_vector(path: Path) -> gpd.GeoDataFrame:
    """Read any OGR vector file as EPSG:4326 (GeoJSON without CRS is WGS84 by spec)."""
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(STORAGE_CRS)
    return gdf.to_crs(STORAGE_CRS)


def write_geojson(gdf: gpd.GeoDataFrame, path: Path) -> Path:
    """Write a FeatureCollection in EPSG:4326 (creates parent dirs, works for empty frames)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf = gdf.to_crs(STORAGE_CRS) if gdf.crs is not None else gdf.set_crs(STORAGE_CRS)
    if len(gdf) == 0:
        # pyogrio cannot infer a geometry type for empty frames; write a minimal valid file.
        path.write_text(
            json.dumps({"type": "FeatureCollection", "features": []}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path
    gdf.to_file(path, driver="GeoJSON", engine="pyogrio", layer_options={"RFC7946": "NO"})
    return path


def to_multipolygon(geom: BaseGeometry) -> MultiPolygon | None:
    """Promote polygonal geometry to MultiPolygon; drop non-polygonal parts; None if nothing is left."""
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    if hasattr(geom, "geoms"):  # GeometryCollection after clipping
        polys: list[Polygon] = []
        for part in geom.geoms:
            mp = to_multipolygon(part)
            if mp is not None:
                polys.extend(mp.geoms)
        return MultiPolygon(polys) if polys else None
    return None


def building_tag(row: pd.Series) -> str:
    """Extract the ``building=*`` value from an OSM row (``tags`` dict/JSON string or a column)."""
    tags = row.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = None
    if isinstance(tags, dict):
        value = tags.get("building")
        if value:
            return str(value)
    value = row.get("building")
    if isinstance(value, str) and value:
        return value
    return "yes"


def as_bool(value: Any) -> bool | None:
    """Normalise QGIS/OGR representations of a boolean; None when not interpretable."""
    if hasattr(value, "item") and not isinstance(value, str):  # numpy scalars (bool_, int64, ...)
        value = value.item()
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, int | float):
        return bool(int(value)) if value in (0, 1) else None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "t", "1", "yes", "y"}:
            return True
        if v in {"false", "f", "0", "no", "n", ""}:
            return False
    return None


def stable_json(obj: Any, relative_paths: bool = True) -> str:
    if relative_paths:
        obj = repo_relative(obj)
    return (
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False, default=_json_default) + "\n"
    )


def _json_default(o: Any) -> Any:
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "item"):  # numpy scalars
        return o.item()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")
