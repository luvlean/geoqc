"""``geoqc cv export-dataset``: labelled tiles -> YOLO-seg txt polygons and U-Net masks.

The training labels come from ``dataset.label_split`` (``train`` since D-021; ``val`` before it).
With ``dataset.holdout_split`` set, all label-split tiles go to ``train`` and the tiles of the
holdout split (``val``) with their own labels form ``holdout`` (early stopping, threshold);
without it the label-split tiles are split deterministically (seeded, stratified by ``stratum``)
into ``train`` and ``holdout`` (default 80/20). ``test`` labels are never read.

Output layout ``data/interim/dataset/``::

    images/{train,holdout}/<tile_id>.png     RGB uint8
    labels/{train,holdout}/<tile_id>.txt     YOLO-seg: "0 x1 y1 x2 y2 ..." normalised [0, 1]
    masks/{train,holdout}/<tile_id>.png      binary 0/255 for U-Net
    data.yaml  split.csv  meta.json
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import yaml
from rasterio import features
from rasterio.transform import Affine
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry

from geoqc.config import resolve_path
from geoqc.cv.tiles import filter_by_split, load_tile_index, read_split_csv, read_tile
from geoqc.cv.vectorize import polygons_only

LABEL_SPLIT = "val"  # default label split when cv.dataset.label_split is not set
FORBIDDEN_SPLIT = "test"  # labels of this split are never read by the cv module
SUBSETS = ("train", "holdout")


@dataclass
class DatasetSummary:
    out_dir: str
    labels_path: str
    seed: int
    holdout_fraction: float
    n_train: int
    n_holdout: int
    n_instances_train: int
    n_instances_holdout: int
    n_tiles_without_labels: int
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assert_not_forbidden(path: str | Path) -> Path:
    """Refuse any path that points into the held-out test labels directory."""
    p = Path(resolve_path(path))
    parts = [s.lower() for s in p.parts]
    for a, b in zip(parts, parts[1:], strict=False):
        if a == "labels" and b == FORBIDDEN_SPLIT:
            raise PermissionError(f"reading labels of split {FORBIDDEN_SPLIT!r} is forbidden: {p}")
    return p


def split_tiles(
    tile_ids: list[str],
    *,
    holdout_fraction: float = 0.2,
    seed: int = 42,
    strata: dict[str, str] | None = None,
) -> dict[str, str]:
    """Deterministic train/holdout assignment (stratified when ``strata`` is given)."""
    if not 0.0 <= holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be in [0, 1)")
    ids = sorted(set(map(str, tile_ids)))
    assignment: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    for tid in ids:
        groups.setdefault((strata or {}).get(tid, "_all"), []).append(tid)
    rng = random.Random(seed)
    for key in sorted(groups):
        members = sorted(groups[key])
        rng.shuffle(members)
        n_hold = int(math.floor(holdout_fraction * len(members) + 0.5))
        if len(members) >= 2 and holdout_fraction > 0:
            n_hold = min(max(n_hold, 1), len(members) - 1)
        else:
            n_hold = 0
        for i, tid in enumerate(members):
            assignment[tid] = "holdout" if i < n_hold else "train"
    if len(ids) >= 2 and holdout_fraction > 0 and "holdout" not in assignment.values():
        assignment[ids[0]] = "holdout"
    return assignment


def load_labels(
    path: str | Path | None = None, *, require_verified: bool = True
) -> gpd.GeoDataFrame:
    """Read ``data/labels/val/buildings.geojson`` (EPSG:4326) keeping polygonal geometries."""
    p = assert_not_forbidden(path or f"data/labels/{LABEL_SPLIT}/buildings.geojson")
    gdf = gpd.read_file(p)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    if require_verified:
        if "verified" not in gdf.columns:
            raise ValueError(
                f"{p} has no 'verified' column; pass require_verified=False for drafts"
            )
        gdf = gdf[gdf["verified"].astype(str).str.lower().isin(["true", "1"])]
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf = gdf[gdf.geom_type.isin(["Polygon", "MultiPolygon"])]
    return gdf.reset_index(drop=True)


MIN_PART_PX = 1.0  # min width/height of a YOLO-seg part, px


def _pixel_ring(coords: np.ndarray, inv: Affine, width: int, height: int) -> np.ndarray:
    xs, ys = coords[:, 0], coords[:, 1]
    cols, rows = inv @ (xs, ys)
    pts = np.stack([np.asarray(cols) / width, np.asarray(rows) / height], axis=1)
    return np.clip(pts, 0.0, 1.0)


def polygon_to_yolo_lines(
    geom: BaseGeometry, transform: Affine, width: int, height: int, class_id: int = 0
) -> list[str]:
    """Exterior rings of a (Multi)Polygon -> YOLO-seg lines with normalised coordinates."""
    geom = polygons_only(geom)
    if geom.is_empty:
        return []
    parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    inv = ~transform
    lines: list[str] = []
    for poly in parts:
        if not isinstance(poly, Polygon) or poly.is_empty:
            continue
        ring = np.asarray(poly.exterior.coords)[:-1]
        pts = _pixel_ring(ring, inv, width, height)
        # drop consecutive duplicates that appear after clipping
        keep = np.ones(len(pts), dtype=bool)
        keep[1:] = np.any(np.abs(np.diff(pts, axis=0)) > 1e-9, axis=1)
        pts = pts[keep]
        if len(pts) < 3:
            continue
        # clipping leaves sub-pixel slivers of multipart buildings: no box for YOLO to learn
        if np.ptp(pts[:, 0]) * width < MIN_PART_PX or np.ptp(pts[:, 1]) * height < MIN_PART_PX:
            continue
        lines.append(" ".join([str(class_id), *(f"{v:.6f}" for v in pts.ravel())]))
    return lines


def rasterize_mask(
    geoms: list[BaseGeometry], transform: Affine, shape: tuple[int, int]
) -> np.ndarray:
    """Binary uint8 mask (0/255) of the polygons on the tile grid (holes respected)."""
    shapes = [(g, 1) for g in geoms if g is not None and not g.is_empty]
    if not shapes:
        return np.zeros(shape, dtype=np.uint8)
    arr = features.rasterize(
        shapes, out_shape=shape, transform=transform, fill=0, all_touched=False, dtype="uint8"
    )
    return (arr * 255).astype(np.uint8)


def export_dataset(
    cv_cfg: dict[str, Any],
    pipeline_cfg: dict[str, Any] | None = None,
    *,
    labels_path: str | Path | None = None,
    split_csv: str | Path | None = None,
    tiles_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    out_dir: str | Path | None = None,
    seed: int | None = None,
    holdout_fraction: float | None = None,
    require_verified: bool = True,
    class_name: str = "building",
) -> DatasetSummary:
    """Export the val split as a YOLO-seg dataset plus binary masks."""
    ds_cfg = dict(cv_cfg.get("dataset") or {})
    seed = int(seed if seed is not None else ds_cfg.get("seed", cv_cfg.get("seed", 42)))
    frac = float(
        holdout_fraction if holdout_fraction is not None else ds_cfg.get("holdout_fraction", 0.2)
    )
    out = Path(resolve_path(out_dir or ds_cfg.get("out_dir", "data/interim/dataset")))
    paths = (pipeline_cfg or {}).get("paths", {}) if pipeline_cfg else {}
    tiles_dir = tiles_dir or paths.get("tiles", "data/tiles")
    index_path = index_path or ((pipeline_cfg or {}).get("tiles", {}) or {}).get("index")

    label_split = str(ds_cfg.get("label_split", LABEL_SPLIT))
    holdout_split = ds_cfg.get("holdout_split")
    if FORBIDDEN_SPLIT in (label_split, holdout_split):
        raise PermissionError(
            f"labels of split {FORBIDDEN_SPLIT!r} are never used by the cv module"
        )
    labels_root = Path(paths.get("labels", "data/labels"))
    split_csv = split_csv or labels_root / "tiles_split.csv"
    labels_file = assert_not_forbidden(
        labels_path or labels_root / label_split / "buildings.geojson"
    )
    labels = load_labels(labels_file, require_verified=require_verified)
    all_tiles = load_tile_index(tiles_dir, index_path)
    split_df = read_split_csv(split_csv)
    index = filter_by_split(all_tiles, label_split, split_csv)
    if len(index) == 0:
        raise ValueError(f"no tiles of split {label_split!r} found in the tile index")
    tile_labels = dict.fromkeys(index["tile_id"].astype(str), labels)
    if holdout_split:
        hold_index = filter_by_split(all_tiles, str(holdout_split), split_csv)
        hold_labels = load_labels(
            assert_not_forbidden(labels_root / str(holdout_split) / "buildings.geojson"),
            require_verified=require_verified,
        )
        assignment = dict.fromkeys(index["tile_id"].astype(str), "train")
        assignment.update(dict.fromkeys(hold_index["tile_id"].astype(str), "holdout"))
        tile_labels.update(dict.fromkeys(hold_index["tile_id"].astype(str), hold_labels))
        index = pd.concat([index, hold_index], ignore_index=True)
        frac = 0.0
    else:
        strata = (
            dict(zip(split_df["tile_id"].astype(str), split_df["stratum"].astype(str), strict=True))
            if "stratum" in split_df.columns
            else None
        )
        assignment = split_tiles(
            list(index["tile_id"]), holdout_fraction=frac, seed=seed, strata=strata
        )

    for sub in SUBSETS:
        for kind in ("images", "labels", "masks"):
            (out / kind / sub).mkdir(parents=True, exist_ok=True)

    counts = {s: 0 for s in SUBSETS}
    instances = {s: 0 for s in SUBSETS}
    n_empty = 0
    metric_cache: dict[int, gpd.GeoDataFrame] = {}
    rows: list[dict[str, Any]] = []
    for row in index.itertuples(index=False):
        tile = read_tile(row.path, str(row.tile_id))
        src = tile_labels[tile.tile_id]
        if id(src) not in metric_cache:
            metric_cache[id(src)] = src.to_crs(tile.crs) if tile.crs is not None else src
        labels_metric = metric_cache[id(src)]
        h, w = tile.shape
        bounds = box(*_bounds_from_transform(tile.transform, w, h))
        cand = labels_metric[labels_metric.intersects(bounds)]
        geoms = [polygons_only(g.intersection(bounds)) for g in cand.geometry]
        geoms = [g for g in geoms if not g.is_empty and g.area > 0]
        sub = assignment[tile.tile_id]
        cv2.imwrite(str(out / "images" / sub / f"{tile.tile_id}.png"), tile.image[:, :, ::-1])
        lines: list[str] = []
        for g in geoms:
            lines.extend(polygon_to_yolo_lines(g, tile.transform, w, h))
        (out / "labels" / sub / f"{tile.tile_id}.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
        cv2.imwrite(
            str(out / "masks" / sub / f"{tile.tile_id}.png"),
            rasterize_mask(geoms, tile.transform, (h, w)),
        )
        counts[sub] += 1
        instances[sub] += len(lines)
        if not lines:
            n_empty += 1
        rows.append(
            {
                "tile_id": tile.tile_id,
                "subset": sub,
                "n_instances": len(lines),
                "path": str(row.path),
            }
        )

    pd.DataFrame(rows).sort_values("tile_id").to_csv(out / "split.csv", index=False)
    data_yaml = {
        "path": str(out),
        "train": "images/train",
        "val": "images/holdout",
        "names": {0: class_name},
        "nc": 1,
    }
    (out / "data.yaml").write_text(yaml.safe_dump(data_yaml, sort_keys=False), encoding="utf-8")
    summary = DatasetSummary(
        out_dir=str(out),
        labels_path=str(labels_file),
        seed=seed,
        holdout_fraction=frac,
        n_train=counts["train"],
        n_holdout=counts["holdout"],
        n_instances_train=instances["train"],
        n_instances_holdout=instances["holdout"],
        n_tiles_without_labels=n_empty,
        generated_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    )
    (out / "meta.json").write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
    return summary


def _bounds_from_transform(
    transform: Affine, width: int, height: int
) -> tuple[float, float, float, float]:
    x0, y0 = transform @ (0, 0)
    x1, y1 = transform @ (width, height)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def read_holdout_tile_ids(out_dir: str | Path | None = None) -> list[str]:
    """Tile ids of the holdout subset from ``split.csv`` written by ``export_dataset``."""
    p = Path(resolve_path(out_dir or "data/interim/dataset")) / "split.csv"
    if not p.exists():
        raise FileNotFoundError(f"{p} not found; run `geoqc cv export-dataset` first")
    df = pd.read_csv(p, dtype={"tile_id": str})
    return sorted(df.loc[df["subset"] == "holdout", "tile_id"].astype(str))
