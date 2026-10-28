"""Shared helpers of the eval module: paths, config defaults, CRS, IO, git commit."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry

from geoqc.config import REPO_ROOT, load_config, resolve_path

DISCREPANCY_CLASSES: tuple[str, ...] = ("missing_in_map", "missing_on_image", "geometry_mismatch")
VERDICTS: tuple[str, ...] = ("map_error", "model_error", "image_issue", "unsure")
STRATA: tuple[str, ...] = ("dense_multistorey", "low_rise_private", "industrial", "mixed")
SPLITS: tuple[str, ...] = ("val", "test")

EVAL_DEFAULTS: dict[str, Any] = {
    "iou_threshold": 0.5,
    "match_method": "greedy",
    "min_area_m2": None,
    "precision_at_n": [10, 20, 50],
    "gate_precision_at_n": 20,
    "sample_size": 50,
    "per_class_min": 15,
    "seed": 42,
    "verdicts": list(VERDICTS),
    "error_analysis": {
        "top_k_worst_tiles": 10,
        "n_example_figures": 6,
        "iou_bins": 10,
        "area_bins_m2": [0, 25, 50, 100, 200, 500, 1000, 5000, 1_000_000],
        "tile_border_buffer_m": 5.0,
        "convexity_threshold": 0.85,
        "shadow_luminance_quantile": 0.25,
        "vegetation_exg_threshold": 20.0,
    },
}


class EvalInputError(RuntimeError):
    """Raised when a required input of an eval command is missing or malformed."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- config / paths
def load_eval_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return eval settings: EVAL_DEFAULTS overlaid by ``config/eval.yaml`` (cfg["eval"])."""
    cfg = cfg if cfg is not None else load_config()
    user = cfg.get("eval") or {}
    merged = json.loads(json.dumps(EVAL_DEFAULTS))
    for key, value in user.items():
        if key == "error_analysis" and isinstance(value, dict):
            merged["error_analysis"].update(value)
        else:
            merged[key] = value
    if merged["min_area_m2"] is None:
        merged["min_area_m2"] = float((cfg.get("annotation") or {}).get("min_area_m2") or 12.0)
    return merged


@dataclass
class EvalPaths:
    """All filesystem inputs/outputs of the module; every field can be overridden from the CLI."""

    repo_root: Path
    reports_dir: Path
    labels_dir: Path
    detections: Path
    tiles_index: Path
    tiles_dir: Path
    secondary: Path
    manifest: Path
    aoi: Path

    @classmethod
    def from_config(
        cls, cfg: dict[str, Any] | None = None, **overrides: Path | str | None
    ) -> EvalPaths:
        cfg = cfg if cfg is not None else load_config()
        paths = cfg.get("paths") or {}
        tiles_cfg = cfg.get("tiles") or {}
        defaults: dict[str, Path] = {
            "repo_root": REPO_ROOT,
            "reports_dir": resolve_path(paths.get("reports", "reports")),
            "labels_dir": resolve_path(paths.get("labels", "data/labels")),
            "detections": resolve_path(paths.get("interim", "data/interim")) / "detections.geojson",
            "tiles_index": resolve_path(tiles_cfg.get("index", "data/tiles/tiles.gpkg")),
            "tiles_dir": resolve_path(paths.get("tiles", "data/tiles")),
            "secondary": resolve_path(paths.get("interim", "data/interim"))
            / "secondary_buildings.geojson",
            "manifest": resolve_path(paths.get("data_dir", "data")) / "manifest.json",
            "aoi": REPO_ROOT / "config" / "aoi.geojson",
        }
        for key, value in overrides.items():
            if value is not None:
                defaults[key] = Path(value)
        return cls(**defaults)

    @property
    def tiles_split(self) -> Path:
        return self.labels_dir / "tiles_split.csv"

    def labels(self, split: str) -> Path:
        return self.labels_dir / split / "buildings.geojson"

    def draft(self, split: str) -> Path:
        return self.labels_dir / split / "draft.geojson"

    def metrics(self, split: str, smoke: bool = False) -> Path:
        return self.reports_dir / (
            f"metrics_{split}_smoke.json" if smoke else f"metrics_{split}.json"
        )

    @property
    def test_runs_log(self) -> Path:
        return self.reports_dir / "metrics_test.runs.log"

    @property
    def figures_dir(self) -> Path:
        return self.reports_dir / "figures"


# --------------------------------------------------------------------------- git / json
def read_git_dirty(repo_root: Path = REPO_ROOT) -> bool | None:
    """True when tracked files outside ``reports/`` differ from HEAD; None without a git binary.

    ``reports/`` is excluded: the eval commands themselves rewrite it before ``gates`` runs.
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no", "--", ".", ":!reports"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(out.strip())


def read_git_commit(repo_root: Path = REPO_ROOT) -> str | None:
    """Return HEAD sha by reading ``.git`` files (no git binary is invoked)."""
    git_path = repo_root / ".git"
    if git_path.is_file():  # worktree: "gitdir: <path>"
        text = git_path.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        git_dir = Path(text.split(":", 1)[1].strip())
        git_dir = git_dir if git_dir.is_absolute() else repo_root / git_dir
    else:
        git_dir = git_path
    head = git_dir / "HEAD"
    if not head.exists():
        return None
    text = head.read_text(encoding="utf-8").strip()
    if not text.startswith("ref:"):
        return text or None
    ref = text.split(":", 1)[1].strip()
    ref_file = git_dir / ref
    if ref_file.exists():
        return ref_file.read_text(encoding="utf-8").strip() or None
    common_dir = git_dir / "commondir"
    if common_dir.exists():  # worktrees keep refs in the main .git
        main = (git_dir / common_dir.read_text(encoding="utf-8").strip()).resolve()
        if (main / ref).exists():
            return (main / ref).read_text(encoding="utf-8").strip() or None
        git_dir = main
    packed = git_dir / "packed-refs"
    if packed.exists():
        for line in packed.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        val = float(obj)
        return None if math.isnan(val) else val
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, float) and math.isnan(obj):
        return None
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def _nan_to_none(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_nan_to_none(v) for v in obj]
    if isinstance(obj, float) and math.isnan(obj):
        return None
    return obj


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(_nan_to_none(payload), fh, indent=2, ensure_ascii=False, default=_json_default)
        fh.write("\n")


def write_csv_atomic(df: pd.DataFrame, path: Path) -> None:
    """Write ``df`` to ``path`` through a temporary file: a crash never leaves half a CSV."""
    tmp = Path(path).with_suffix(Path(path).suffix + ".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)


def read_json(path: Path | None) -> dict[str, Any] | None:
    """Return parsed JSON or None when the file is missing or not a JSON object."""
    if path is None or not Path(path).exists():
        return None
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def pick(data: dict[str, Any] | None, *dotted_paths: str) -> Any:
    """Return the first value found under any of the dotted paths (``"a.b.c"``), else None."""
    if not data:
        return None
    for dotted in dotted_paths:
        node: Any = data
        ok = True
        for key in dotted.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and node is not None:
            return node
    return None


def rel(path: Path, root: Path = REPO_ROOT) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- CRS
def utm_epsg_for(lon: float, lat: float) -> int:
    """UTM zone EPSG for a WGS84 point; prefers geoqc.data.crs.utm_epsg_for when available."""
    try:
        from geoqc.data.crs import utm_epsg_for as data_utm  # type: ignore[import-not-found]

        return int(data_utm(lon, lat))
    except Exception:  # module not implemented yet or different signature
        zone = int(math.floor((lon + 180.0) / 6.0)) + 1
        zone = min(max(zone, 1), 60)
        return (32600 if lat >= 0 else 32700) + zone


def as_epsg_string(code: Any) -> str:
    text = str(code).strip()
    return text.upper() if text.upper().startswith("EPSG:") else f"EPSG:{int(float(text))}"


def resolve_metric_crs(
    cfg: dict[str, Any],
    sample: gpd.GeoSeries | gpd.GeoDataFrame | None = None,
    manifest_path: Path | None = None,
) -> tuple[str, str]:
    """Return (EPSG string, source description) of the metric CRS (CONTRACTS §3.1).

    Order: ``config/pipeline.yaml crs.metric`` -> ``data/manifest.json aoi.utm_epsg`` ->
    UTM zone of the centre of ``sample`` (any CRS) -> EvalInputError.
    """
    metric = (cfg.get("crs") or {}).get("metric")
    if metric:
        return as_epsg_string(metric), "config/pipeline.yaml crs.metric"
    utm = pick(read_json(manifest_path), "aoi.utm_epsg")
    if utm:
        return as_epsg_string(utm), "data/manifest.json aoi.utm_epsg"
    if sample is not None and len(sample) > 0:
        geoms = sample.geometry if isinstance(sample, gpd.GeoDataFrame) else sample
        if geoms.crs is None:
            geoms = geoms.set_crs(4326)
        minx, miny, maxx, maxy = geoms.to_crs(4326).total_bounds
        code = utm_epsg_for((minx + maxx) / 2.0, (miny + maxy) / 2.0)
        return f"EPSG:{code}", "derived from data centroid (UTM zone)"
    raise EvalInputError(
        "metric CRS is unknown: set config/pipeline.yaml crs.metric (`make data` fills it) or provide data"
    )


# --------------------------------------------------------------------------- geometry IO
def to_polygonal(geom: BaseGeometry | None) -> BaseGeometry | None:
    """make_valid + keep only the polygonal part; None for empty/non-areal geometries."""
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = make_valid(geom)
    if isinstance(geom, Polygon | MultiPolygon):
        return geom if not geom.is_empty else None
    polys: list[Polygon] = []
    for part in getattr(geom, "geoms", []):
        if isinstance(part, Polygon) and not part.is_empty:
            polys.append(part)
        elif isinstance(part, MultiPolygon):
            polys.extend(p for p in part.geoms if not p.is_empty)
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def clean_polygons(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Return a copy with valid polygonal geometries only (rows with nothing areal are dropped)."""
    if len(gdf) == 0:
        return gdf.copy()
    out = gdf.copy()
    out["geometry"] = gpd.GeoSeries(
        [to_polygonal(g) for g in out.geometry], index=out.index, crs=out.crs
    )
    out = out[out.geometry.notna()]
    return gpd.GeoDataFrame(out, geometry="geometry", crs=gdf.crs)


def read_geojson(path: Path, what: str) -> gpd.GeoDataFrame:
    if not Path(path).exists():
        raise EvalInputError(f"{what} not found: {path}")
    try:
        gdf = gpd.read_file(path)
    except Exception as exc:  # pyogrio raises DataSourceError on empty collections
        if "no features" in str(exc).lower() or "empty" in str(exc).lower():
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        raise EvalInputError(f"cannot read {what} {path}: {exc}") from exc
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    return gdf


def read_tiles_split(path: Path) -> pd.DataFrame:
    """Read ``data/labels/tiles_split.csv`` (CONTRACTS §3.5) and validate its columns."""
    if not Path(path).exists():
        raise EvalInputError(f"tiles split not found: {path} (run `geoqc annotation select-tiles`)")
    df = pd.read_csv(path, dtype={"tile_id": str, "split": str, "stratum": str})
    required = {"tile_id", "split", "stratum"}
    missing = required - set(df.columns)
    if missing:
        raise EvalInputError(f"{path} lacks columns {sorted(missing)}")
    df["tile_id"] = df["tile_id"].astype(str)
    return df


def read_tiles_index(
    tiles_index: Path, tiles_dir: Path, tile_ids: list[str]
) -> gpd.GeoDataFrame | None:
    """Tile polygons (EPSG:4326) for ``tile_ids`` from tiles.gpkg, else from tile GeoTIFF bounds.

    Returns None when neither source is available.
    """
    if Path(tiles_index).exists():
        try:
            gdf = gpd.read_file(tiles_index, layer="tiles")
        except Exception:
            gdf = gpd.read_file(tiles_index)
        if "tile_id" in gdf.columns:
            gdf["tile_id"] = gdf["tile_id"].astype(str)
            gdf = gdf[gdf["tile_id"].isin(tile_ids)]
            if gdf.crs is None:
                gdf = gdf.set_crs(4326)
            if len(gdf) > 0:
                return gpd.GeoDataFrame(gdf[["tile_id", "geometry"]], crs=gdf.crs).to_crs(4326)
    rows: list[dict[str, Any]] = []
    try:
        import rasterio
    except ImportError:  # pragma: no cover
        return None
    for tid in tile_ids:
        tif = Path(tiles_dir) / f"{tid}.tif"
        if not tif.exists():
            continue
        with rasterio.open(tif) as src:
            poly = gpd.GeoSeries([box(*src.bounds)], crs=src.crs).to_crs(4326).iloc[0]
        rows.append({"tile_id": tid, "geometry": poly})
    if not rows:
        return None
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
