"""``geoqc cv prepare-microsoft``: Microsoft building footprints on our own tiles as training labels.

The footprints (``data/interim/secondary_buildings.geojson``, ODbL) are pseudo-labels: drawn from
other satellite imagery, not checked against our image. They are used only on tiles far from the
labelled ones: every ``val`` and ``test`` tile and its 8 neighbours (tiles overlap by 64 px) are
excluded, so neither the honest val check (G3) nor the test set sees a pixel of this material.
Tiles mostly outside the image (nodata) or without a footprint are skipped. A seeded share of the
kept tiles is the Ultralytics validation subset, so val is not used for early stopping either.

Output layout (same as ``export-dataset``)::

    <out_dir>/images/{train,holdout}/<tile_id>.png  labels/...txt  masks/...png
    data.yaml  split.csv  meta.json
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import yaml
from shapely.geometry import box

from geoqc.config import resolve_path
from geoqc.cv.dataset import (
    SUBSETS,
    _bounds_from_transform,
    polygon_to_yolo_lines,
    rasterize_mask,
    split_tiles,
)
from geoqc.cv.tiles import load_tile_index, read_split_csv, read_tile
from geoqc.cv.vectorize import polygons_only

LABELLED_SPLITS = ("train", "val", "test")


@dataclass
class PseudoSummary:
    out_dir: str
    footprints_path: str
    n_candidates: int
    n_excluded_near_labels: int
    n_skipped_nodata: int
    n_skipped_empty: int
    n_train: int
    n_holdout: int
    n_instances_train: int
    n_instances_holdout: int
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_col(tile_id: str) -> tuple[int, int]:
    r, c = str(tile_id).split("_")
    return int(r), int(c)


def excluded_tiles(split_df: pd.DataFrame, radius: int = 1) -> set[str]:
    """Tiles of val/test and every tile within ``radius`` rows/cols of them."""
    labelled = split_df[split_df["split"].isin(LABELLED_SPLITS)]["tile_id"].astype(str)
    out: set[str] = set()
    for tid in labelled:
        r, c = _row_col(tid)
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                out.add(f"{r + dr}_{c + dc}")
    return out


def export_microsoft_dataset(
    cv_cfg: dict[str, Any],
    pipeline_cfg: dict[str, Any] | None = None,
    *,
    footprints_path: str | Path | None = None,
    split_csv: str | Path | None = None,
    tiles_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    out_dir: str | Path | None = None,
) -> PseudoSummary:
    """Write a YOLO-seg dataset of Microsoft footprints on tiles away from val/test."""
    pcfg = dict(cv_cfg.get("microsoft") or {})
    seed = int(pcfg.get("seed", cv_cfg.get("seed", 42)))
    frac = float(pcfg.get("holdout_fraction", 0.2))
    min_area = float(pcfg.get("min_area_m2", 12))
    min_valid = float(pcfg.get("min_valid_fraction", 0.9))
    radius = int(pcfg.get("exclude_radius", 1))
    out = Path(resolve_path(out_dir or pcfg.get("out_dir", "data/interim/external/microsoft")))
    paths = (pipeline_cfg or {}).get("paths", {}) if pipeline_cfg else {}
    tiles_dir = tiles_dir or paths.get("tiles", "data/tiles")
    index_path = index_path or ((pipeline_cfg or {}).get("tiles", {}) or {}).get("index")
    split_csv = split_csv or Path(paths.get("labels", "data/labels")) / "tiles_split.csv"
    fp_path = Path(
        resolve_path(
            footprints_path or pcfg.get("footprints", "data/interim/secondary_buildings.geojson")
        )
    )

    footprints = gpd.read_file(fp_path)
    footprints = footprints[footprints.geometry.notna() & ~footprints.geometry.is_empty]
    index = load_tile_index(tiles_dir, index_path)
    blocked = excluded_tiles(read_split_csv(split_csv), radius)
    candidates = index[~index["tile_id"].astype(str).isin(blocked)]

    kept: list[tuple[Any, list]] = []
    n_nodata = n_empty = 0
    fp_metric: gpd.GeoDataFrame | None = None
    for row in candidates.itertuples(index=False):
        tile = read_tile(row.path, str(row.tile_id))
        if fp_metric is None:
            fp_metric = footprints.to_crs(tile.crs) if tile.crs is not None else footprints
        valid = float(np.any(tile.image > 0, axis=2).mean())
        if valid < min_valid:
            n_nodata += 1
            continue
        h, w = tile.shape
        bounds = box(*_bounds_from_transform(tile.transform, w, h))
        geoms = [
            polygons_only(g.intersection(bounds))
            for g in fp_metric[fp_metric.intersects(bounds)].geometry
        ]
        geoms = [g for g in geoms if not g.is_empty and g.area >= min_area]
        if not geoms:
            n_empty += 1
            continue
        kept.append((tile, geoms))

    assignment = split_tiles([t.tile_id for t, _ in kept], holdout_fraction=frac, seed=seed)
    for sub in SUBSETS:
        for kind in ("images", "labels", "masks"):
            (out / kind / sub).mkdir(parents=True, exist_ok=True)
    counts = {s: 0 for s in SUBSETS}
    instances = {s: 0 for s in SUBSETS}
    rows: list[dict[str, Any]] = []
    for tile, geoms in kept:
        h, w = tile.shape
        sub = assignment[tile.tile_id]
        lines: list[str] = []
        for g in geoms:
            lines.extend(polygon_to_yolo_lines(g, tile.transform, w, h))
        cv2.imwrite(str(out / "images" / sub / f"{tile.tile_id}.png"), tile.image[:, :, ::-1])
        (out / "labels" / sub / f"{tile.tile_id}.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
        cv2.imwrite(
            str(out / "masks" / sub / f"{tile.tile_id}.png"),
            rasterize_mask(geoms, tile.transform, (h, w)),
        )
        counts[sub] += 1
        instances[sub] += len(lines)
        rows.append({"tile_id": tile.tile_id, "subset": sub, "n_instances": len(lines)})

    pd.DataFrame(rows, columns=["tile_id", "subset", "n_instances"]).sort_values("tile_id").to_csv(
        out / "split.csv", index=False
    )
    data_yaml = {
        "path": str(out),
        "train": "images/train",
        "val": "images/holdout",
        "names": {0: "building"},
        "nc": 1,
    }
    (out / "data.yaml").write_text(yaml.safe_dump(data_yaml, sort_keys=False), encoding="utf-8")
    summary = PseudoSummary(
        out_dir=str(out),
        footprints_path=str(fp_path),
        n_candidates=len(candidates),
        n_excluded_near_labels=len(index) - len(candidates),
        n_skipped_nodata=n_nodata,
        n_skipped_empty=n_empty,
        n_train=counts["train"],
        n_holdout=counts["holdout"],
        n_instances_train=instances["train"],
        n_instances_holdout=instances["holdout"],
        generated_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    )
    meta = {
        **summary.to_dict(),
        "license": "ODbL (Microsoft GlobalMLBuildingFootprints)",
        "excluded_splits": list(LABELLED_SPLITS),
        "exclude_radius": radius,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return summary
