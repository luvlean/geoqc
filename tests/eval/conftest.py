"""Синтетическое окружение для тестов eval: 3 тайла (2 val, 1 test), разметка, детекции, растры."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import yaml
from shapely.affinity import translate
from shapely.geometry import Polygon, box

from geoqc.config import REPO_ROOT
from geoqc.eval.common import EvalPaths

METRIC_CRS = "EPSG:32637"
X0, Y0 = 410_000.0, 6_175_000.0
MODEL_VERSION = "synthetic-00000000"

# tile_id -> (x1, y1, x2, y2) in metres relative to (X0, Y0); 256 m tiles with a 64 m overlap.
TILES = {"0_0": (0, 0, 256, 256), "0_1": (192, 0, 448, 256), "1_0": (0, 192, 256, 448)}
STRATA = {"0_0": "dense_multistorey", "0_1": "low_rise_private", "1_0": "industrial"}
SPLIT = {"0_0": "val", "0_1": "val", "1_0": "test"}


def m(x1: float, y1: float, x2: float, y2: float) -> Polygon:
    return box(X0 + x1, Y0 + y1, X0 + x2, Y0 + y2)


L_SHAPE = translate(
    Polygon([(150, 150), (170, 150), (170, 158), (158, 158), (158, 170), (150, 170)]), X0, Y0
)

# name, tile_id, geometry
GT_VAL = [
    ("A", "0_0", m(10, 10, 30, 30)),  # matched with IoU 0.818
    ("B", "0_0", m(50, 50, 60, 70)),  # exact match
    ("C", "0_0", L_SHAPE),  # complex roof (area 256, hull 328), matched by its bbox with IoU 0.64
    ("D", "0_1", m(300, 100, 320, 120)),  # exact match
    ("E", "0_1", m(430, 200, 445, 215)),  # FN, 3 m from the tile edge
]
GT_TEST = [("H", "1_0", m(20, 300, 40, 320))]
# name, tile_id property, score, geometry
DETS = [
    ("A'", "0_0", 0.9, m(12, 10, 32, 30)),
    ("B'", "0_0", 0.8, m(50, 50, 60, 70)),
    ("C'", "0_0", 0.7, m(150, 150, 170, 170)),
    ("D'", "0_1", 0.95, m(300, 100, 320, 120)),
    ("F", "9_9", 0.6, m(100, 100, 110, 110)),  # FP; wrong tile_id property, spatially in 0_0
    ("H'", "1_0", 0.85, m(20, 300, 40, 320)),
    ("Z", None, 0.5, m(1000, 1000, 1010, 1010)),  # outside every tile
]
SECONDARY = [m(21, 300, 41, 320), m(100, 300, 110, 310)]  # H shifted 1 m (IoU 0.905) + extra


@dataclass
class SyntheticEnv:
    root: Path
    paths: EvalPaths
    cfg: dict
    cli_args: list[str]
    discrepancies_geojson: Path
    secondary_geojson: Path
    draft_val: Path


def _write_tiles(root: Path) -> None:
    rows = []
    for tid, (x1, y1, x2, y2) in TILES.items():
        r, c = (int(v) for v in tid.split("_"))
        rows.append(
            {
                "tile_id": tid,
                "row": r,
                "col": c,
                "path": f"data/tiles/{tid}.tif",
                "geometry": m(x1, y1, x2, y2),
            }
        )
    tiles = gpd.GeoDataFrame(rows, geometry="geometry", crs=METRIC_CRS).to_crs(4326)
    (root / "tiles").mkdir(parents=True, exist_ok=True)
    tiles.to_file(root / "tiles" / "tiles.gpkg", layer="tiles", driver="GPKG")
    pd.DataFrame(
        [
            {
                "tile_id": t,
                "split": SPLIT[t],
                "stratum": STRATA[t],
                "row": int(t.split("_")[0]),
                "col": int(t.split("_")[1]),
            }
            for t in TILES
        ]
    ).to_csv(root / "labels" / "tiles_split.csv", index=False)


def _labels_gdf(items, split: str, verified: bool) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "name": [n for n, _, _ in items],
            "tile_id": [t for _, t, _ in items],
            "split": split,
            "source": "manual" if verified else "pre_annotation",
            "verified": verified,
            "annotator": "synthetic",
            "notes": "",
        },
        geometry=[g for _, _, g in items],
        crs=METRIC_CRS,
    ).to_crs(4326)


def _write_rasters(root: Path) -> None:
    import rasterio
    from rasterio.transform import from_origin

    for tid in ("0_0", "0_1"):
        x1, y1, x2, y2 = TILES[tid]
        img = np.full((3, 512, 512), 120, dtype=np.uint8)
        if tid == "0_1":  # FN "E" (x 430-445, y 200-215) in shadow
            img[:, 82:112, 476:506] = 20
        if tid == "0_0":  # FP "F" (x 100-110, y 100-110) on vegetation
            img[0, 292:312, 200:220] = 50
            img[1, 292:312, 200:220] = 200
            img[2, 292:312, 200:220] = 50
        with rasterio.open(
            root / "tiles" / f"{tid}.tif",
            "w",
            driver="GTiff",
            height=512,
            width=512,
            count=3,
            dtype="uint8",
            crs=METRIC_CRS,
            transform=from_origin(X0 + x1, Y0 + y2, 0.5, 0.5),
            nodata=0,
        ) as dst:
            dst.write(img)


def _write_discrepancies(path: Path, n: int = 90) -> None:
    rng = np.random.default_rng(0)
    classes = ["missing_in_map"] * 45 + ["geometry_mismatch"] * 30 + ["missing_on_image"] * 15
    geoms = [m(5 * i, 600, 5 * i + 4, 604) for i in range(n)]
    gdf = gpd.GeoDataFrame(
        {
            "id": np.arange(1, n + 1),
            "class": classes,
            "confidence": np.round(rng.uniform(0.05, 0.99, n), 4),
            "iou": np.round(rng.uniform(0.0, 0.5, n), 3),
            "status": "new",
            "ref_osm_id": None,
            "model_version": MODEL_VERSION,
            "created_at": "2026-09-26T00:00:00Z",
        },
        geometry=geoms,
        crs=METRIC_CRS,
    ).to_crs(4326)
    gdf.to_file(path, driver="GeoJSON")


@pytest.fixture
def synthetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SyntheticEnv:
    """Полный набор входов §3.5/§3.6 во временном каталоге + изолированный config dir."""
    root = tmp_path
    for sub in ("labels/val", "labels/test", "interim", "reports", "config"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    _write_tiles(root)
    _labels_gdf(GT_VAL, "val", True).to_file(
        root / "labels" / "val" / "buildings.geojson", driver="GeoJSON"
    )
    _labels_gdf(GT_VAL, "val", False).to_file(
        root / "labels" / "val" / "draft.geojson", driver="GeoJSON"
    )
    _labels_gdf(GT_TEST, "test", True).to_file(
        root / "labels" / "test" / "buildings.geojson", driver="GeoJSON"
    )
    dets = gpd.GeoDataFrame(
        {
            "name": [n for n, _, _, _ in DETS],
            "tile_id": [t for _, t, _, _ in DETS],
            "score": [s for _, _, s, _ in DETS],
            "model_version": MODEL_VERSION,
        },
        geometry=[g for _, _, _, g in DETS],
        crs=METRIC_CRS,
    ).to_crs(4326)
    dets.to_file(root / "interim" / "detections.geojson", driver="GeoJSON")
    secondary = gpd.GeoDataFrame(
        {"source": ["microsoft"] * len(SECONDARY)}, geometry=SECONDARY, crs=METRIC_CRS
    ).to_crs(4326)
    secondary.to_file(root / "interim" / "secondary_buildings.geojson", driver="GeoJSON")
    _write_rasters(root)
    disc = root / "interim" / "discrepancies_synth.geojson"
    _write_discrepancies(disc)

    # isolated config: pipeline.yaml with the metric CRS, eval.yaml and quality_gates.yaml from the repo
    cfg_dir = root / "config"
    pipeline = yaml.safe_load((REPO_ROOT / "config" / "pipeline.yaml").read_text(encoding="utf-8"))
    pipeline["crs"]["metric"] = METRIC_CRS
    (cfg_dir / "pipeline.yaml").write_text(yaml.safe_dump(pipeline), encoding="utf-8")
    shutil.copy(REPO_ROOT / "config" / "eval.yaml", cfg_dir / "eval.yaml")
    shutil.copy(REPO_ROOT / "config" / "quality_gates.yaml", cfg_dir / "quality_gates.yaml")
    monkeypatch.setenv("GEOQC_CONFIG_DIR", str(cfg_dir))
    from geoqc.config import load_config

    cfg = load_config(cfg_dir)
    paths = EvalPaths(
        repo_root=root,
        reports_dir=root / "reports",
        labels_dir=root / "labels",
        detections=root / "interim" / "detections.geojson",
        tiles_index=root / "tiles" / "tiles.gpkg",
        tiles_dir=root / "tiles",
        secondary=root / "interim" / "secondary_buildings.geojson",
        manifest=root / "manifest.json",
        aoi=root / "aoi.geojson",
    )
    cli_args = [
        "--reports-dir",
        str(paths.reports_dir),
        "--labels-dir",
        str(paths.labels_dir),
        "--tiles-index",
        str(paths.tiles_index),
        "--tiles-dir",
        str(paths.tiles_dir),
        "--detections",
        str(paths.detections),
    ]
    return SyntheticEnv(
        root, paths, cfg, cli_args, disc, paths.secondary, root / "labels" / "val" / "draft.geojson"
    )
