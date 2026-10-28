"""Synthetic fixtures for the CV module: two overlapping UTM tiles with bright squares.

Layout of the synthetic AOI (EPSG:32637, 0.5 m/px, tiles 512 px with a 64 px overlap):

* tile ``0_0`` covers x in [500000, 500256], tile ``0_1`` covers x in [500224, 500480];
* square A (30 x 30 m, brightness 255) straddles the overlap strip -> appears in both tiles;
* square B (20 x 20 m, brightness 255) lies only in tile ``0_0``;
* square C (24 x 24 m, brightness 220) lies only in tile ``0_1``;
* a 2 x 2 px speck (1 m2) in tile ``0_0`` must be removed by the min-area filter;
* tile ``1_0`` (split ``test``) is dark: no buildings, no labels for it are ever created.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
import yaml
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

CRS_UTM = "EPSG:32637"
PX = 0.5
SIZE = 512
OVERLAP = 64
STEP_M = (SIZE - OVERLAP) * PX  # 224 m
X0, Y0 = 500000.0, 6000000.0

SQUARES = {
    # name: (minx, miny, maxx, maxy, brightness)
    "A": (500210.0, 5999900.0, 500240.0, 5999930.0, 255),
    "B": (500050.0, 5999800.0, 500070.0, 5999820.0, 255),
    "C": (500300.0, 5999850.0, 500324.0, 5999874.0, 220),
}
SPECK = (500100.0, 5999780.0, 500101.0, 5999781.0, 255)


def _tile_transform(row: int, col: int):
    return from_origin(X0 + col * STEP_M, Y0 - row * STEP_M, PX, PX)


def _paint(img: np.ndarray, transform, rect: tuple[float, float, float, float, int]) -> None:
    minx, miny, maxx, maxy, value = rect
    inv = ~transform
    c0, r0 = inv @ (minx, maxy)
    c1, r1 = inv @ (maxx, miny)
    c0, c1 = int(round(min(c0, c1))), int(round(max(c0, c1)))
    r0, r1 = int(round(min(r0, r1))), int(round(max(r0, r1)))
    c0, c1 = max(c0, 0), min(c1, img.shape[1])
    r0, r1 = max(r0, 0), min(r1, img.shape[0])
    if c1 > c0 and r1 > r0:
        img[r0:r1, c0:c1, :] = value


def write_tile(path: Path, row: int, col: int, rects: list, background: int = 30) -> None:
    transform = _tile_transform(row, col)
    img = np.full((SIZE, SIZE, 3), background, dtype=np.uint8)
    for rect in rects:
        _paint(img, transform, rect)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=SIZE,
        width=SIZE,
        count=3,
        dtype="uint8",
        crs=CRS_UTM,
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(np.transpose(img, (2, 0, 1)))


@pytest.fixture
def synthetic_aoi(tmp_path: Path) -> dict:
    """Create tiles, tiles.gpkg, val labels, tiles_split.csv and a config dir under tmp_path."""
    data = tmp_path / "data"
    tiles_dir = data / "tiles"
    labels_dir = data / "labels"
    val_dir = labels_dir / "val"
    hidden_dir = labels_dir / "test"  # exists only to prove export never touches it
    for d in (tiles_dir, val_dir, hidden_dir, data / "interim", tmp_path / "reports"):
        d.mkdir(parents=True, exist_ok=True)

    rects_all = [*SQUARES.values(), SPECK]
    spec = {
        "0_0": (0, 0, "val", "low_rise_private"),
        "0_1": (0, 1, "val", "mixed"),
        "1_0": (1, 0, "test", "mixed"),
    }
    rows = []
    for tid, (r, c, split, stratum) in spec.items():
        p = tiles_dir / f"{r}_{c}.tif"
        write_tile(p, r, c, rects_all if r == 0 else [])
        with rasterio.open(p) as src:
            b = src.bounds
        rows.append(
            {
                "tile_id": tid,
                "row": r,
                "col": c,
                "path": str(p),
                "geometry": box(*b),
                "split": split,
                "stratum": stratum,
            }
        )
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS_UTM).to_crs("EPSG:4326")
    gdf[["tile_id", "row", "col", "path", "geometry"]].to_file(
        tiles_dir / "tiles.gpkg", driver="GPKG", layer="tiles"
    )
    gdf[["tile_id", "split", "stratum", "row", "col"]].to_csv(
        labels_dir / "tiles_split.csv", index=False
    )

    feats = []
    for name, (minx, miny, maxx, maxy, _) in SQUARES.items():
        geom = gpd.GeoSeries([box(minx, miny, maxx, maxy)], crs=CRS_UTM).to_crs("EPSG:4326").iloc[0]
        tile_id = "0_1" if name == "C" else "0_0"
        feats.append(
            {
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": {
                    "tile_id": tile_id,
                    "split": "val",
                    "source": "manual",
                    "verified": True,
                    "annotator": "test",
                    "notes": name,
                },
            }
        )
    (val_dir / "buildings.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    (hidden_dir / "buildings.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    pipeline = {
        "project": "geoqc-test",
        "crs": {"storage": "EPSG:4326", "metric": CRS_UTM},
        "paths": {
            "data_dir": str(data),
            "interim": str(data / "interim"),
            "tiles": str(tiles_dir),
            "labels": str(labels_dir),
            "models": str(tmp_path / "models"),
            "reports": str(tmp_path / "reports"),
        },
        "tiles": {"size_px": SIZE, "overlap_px": OVERLAP, "index": str(tiles_dir / "tiles.gpkg")},
        "random_seed": 42,
    }
    cv = {
        "backend": "mock",
        "model_name": "mock",
        "weights": {
            "path": str(tmp_path / "models" / "none.pt"),
            "hf_repo": None,
            "sha256": None,
            "is_fallback": False,
        },
        "model_version": "mock-00000000",
        "conf_threshold": 0.5,
        "imgsz": SIZE,
        "batch": 2,
        "device": "cpu",
        "seed": 42,
        "mock": {"threshold": 200, "min_component_px": 1, "base_score": 0.5},
        "vectorize": {
            "simplify_tolerance_m": 0.5,
            "min_area_m2": 12,
            "tile_overlap_merge_iou": 0.1,
            "min_overlap_ratio": 0.5,
            "score_mode": "max",
            "merge_same_tile": False,
            "drop_nodata": True,
        },
        "dataset": {
            "out_dir": str(data / "interim" / "dataset"),
            "holdout_fraction": 0.5,
            "seed": 42,
        },
        "sweep": {
            "conf_floor": 0.05,
            "thresholds": [0.5, 0.9, 0.95, 0.99],
            "iou_threshold": 0.5,
            "report": str(tmp_path / "reports" / "threshold_sweep_val.json"),
        },
    }
    (cfg_dir / "pipeline.yaml").write_text(
        yaml.safe_dump(pipeline, sort_keys=False), encoding="utf-8"
    )
    (cfg_dir / "cv.yaml").write_text(yaml.safe_dump(cv, sort_keys=False), encoding="utf-8")
    (cfg_dir / "aoi.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    return {
        "root": tmp_path,
        "data": data,
        "tiles_dir": tiles_dir,
        "index": tiles_dir / "tiles.gpkg",
        "labels_dir": labels_dir,
        "labels": val_dir / "buildings.geojson",
        "split_csv": labels_dir / "tiles_split.csv",
        "config_dir": cfg_dir,
        "pipeline": pipeline,
        "cv": cv,
        "crs": CRS_UTM,
    }


@pytest.fixture
def env_config(synthetic_aoi: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Point geoqc.config.load_config() at the synthetic config dir (for CLI tests)."""
    monkeypatch.setenv("GEOQC_CONFIG_DIR", str(synthetic_aoi["config_dir"]))
    return synthetic_aoi
