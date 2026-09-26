"""Synthetic fixtures for the annotation module: a UTM tile grid, tagged OSM buildings, a secondary source.

The world lives in EPSG:32637 (UTM 37N) around (400000, 6000000). Tiles: 12x12 grid, 256 m
(512 px @ 0.5 m) with 32 m overlap (64 px), as in CONTRACTS §3.3. Quadrants (HALF = 6):

* rows 0-5 / cols 0-5:   ``building=apartments`` 60x15 m blocks          -> dense_multistorey
* rows 0-5 / cols 6-11:  ``building=house`` 10x8 m                       -> low_rise_private
* rows 6-11 / cols 0-5:  ``building=industrial`` 40x30 m                 -> industrial
* rows 6-11 / cols 6-11: apartments / houses / industrial, 1/3 each      -> mixed
* tiles (11,11), (0,11), (5,5) have no buildings                        -> excluded
Buildings never overlap each other (10 m gaps) and stay out of the 32 m overlap strip.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import yaml
from shapely.affinity import translate
from shapely.geometry import box

from geoqc.annotation.common import STORAGE_CRS, Paths
from geoqc.config import CONFIG_DIR, load_config

METRIC = "EPSG:32637"
X0, Y0 = 400000.0, 6000000.0
TILE_M = 256.0
STRIDE_M = 224.0
GRID = 12
HALF = 6
EMPTY_TILES = {(11, 11), (0, 11), (5, 5)}


def _tiles_metric() -> gpd.GeoDataFrame:
    rows = []
    for r in range(GRID):
        for c in range(GRID):
            x = X0 + c * STRIDE_M
            y = Y0 - r * STRIDE_M
            rows.append(
                {
                    "tile_id": f"{r}_{c}",
                    "row": r,
                    "col": c,
                    "path": f"data/tiles/{r}_{c}.tif",
                    "geometry": box(x, y - TILE_M, x + TILE_M, y),
                }
            )
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=METRIC)


def _buildings_metric(tiles: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    rng = np.random.default_rng(7)
    feats = []
    osm_id = 1
    core = TILE_M - 80  # usable width inside the tile core
    for _, t in tiles.iterrows():
        r, c = int(t["row"]), int(t["col"])
        if (r, c) in EMPTY_TILES:
            continue
        minx, miny, maxx, maxy = t.geometry.bounds
        cx0, cy0 = (
            minx + 40,
            miny + 40,
        )  # keep buildings inside the core (outside the overlap strip)
        if r < HALF and c < HALF:
            groups = [("apartments", 60, 15, 4)]
        elif r < HALF:
            groups = [("house", 10, 8, 12)]
        elif c < HALF:
            groups = [("industrial", 40, 30, 3)]
        else:  # 480 m² of each group -> every share is exactly 1/3 < dominant_share
            groups = [("apartments", 40, 12, 1), ("house", 10, 8, 6), ("industrial", 24, 20, 1)]
        gy = cy0
        for tag, w, h, count in groups:
            per_row = max(1, int(core // (w + 10)))
            for k in range(count):
                gx = cx0 + (k % per_row) * (w + 10) + rng.uniform(0, 3)
                y = gy + (k // per_row) * (h + 10) + rng.uniform(0, 3)
                feats.append(
                    {
                        "osm_id": osm_id,
                        "tags": {"building": tag},
                        "snapshot_date": "2026-09-01",
                        "geometry": box(gx, y, gx + w, y + h),
                    }
                )
                osm_id += 1
            gy += ((count - 1) // per_row + 1) * (h + 10) + 5
    return gpd.GeoDataFrame(feats, geometry="geometry", crs=METRIC)


@pytest.fixture(scope="session")
def synth_world(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Synthetic tiles.gpkg, osm_buildings.geojson and secondary_buildings.geojson on disk (EPSG:4326)."""
    root = tmp_path_factory.mktemp("synth")
    tiles_m = _tiles_metric()
    osm_m = _buildings_metric(tiles_m)
    tiles_path = root / "tiles" / "tiles.gpkg"
    tiles_path.parent.mkdir()
    tiles_m.to_crs(STORAGE_CRS).to_file(tiles_path, layer="tiles", driver="GPKG")
    interim = root / "interim"
    interim.mkdir()
    osm = osm_m.to_crs(STORAGE_CRS).copy()
    osm["tags"] = osm["tags"].apply(lambda d: __import__("json").dumps(d))
    osm_path = interim / "osm_buildings.geojson"
    osm.to_file(osm_path, driver="GeoJSON")
    # secondary source: same buildings shifted by 1 m, 5 % scaled, one extra false positive per quadrant
    sec = osm_m.copy()
    sec["geometry"] = [
        translate(g, 1.0, -1.0).buffer(0.3, join_style="mitre") for g in sec.geometry
    ]
    sec = sec[["geometry"]]
    sec_path = interim / "secondary_buildings.geojson"
    sec.to_crs(STORAGE_CRS).to_file(sec_path, driver="GeoJSON")
    return {
        "root": root,
        "tiles_path": tiles_path,
        "osm_path": osm_path,
        "secondary_path": sec_path,
        "tiles_m": tiles_m,
        "osm_m": osm_m,
    }


@pytest.fixture(scope="session")
def config_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy of config/ with crs.metric pinned to the synthetic UTM zone and no aoi.geojson."""
    d = tmp_path_factory.mktemp("config")
    for name in ("pipeline.yaml", "annotation.yaml", "quality_gates.yaml"):
        shutil.copyfile(CONFIG_DIR / name, d / name)
    pipeline = yaml.safe_load((d / "pipeline.yaml").read_text(encoding="utf-8"))
    pipeline["crs"]["metric"] = METRIC
    (d / "pipeline.yaml").write_text(yaml.safe_dump(pipeline), encoding="utf-8")
    return d


@pytest.fixture
def cfg(config_dir: Path) -> dict:
    return copy.deepcopy(load_config(config_dir))


@pytest.fixture
def cli_env(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at the synthetic config dir."""
    monkeypatch.setenv("GEOQC_CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.fixture
def paths(synth_world: dict, tmp_path: Path, config_dir: Path) -> Paths:
    labels = tmp_path / "labels"
    labels.mkdir()
    return Paths(
        labels_dir=labels,
        tiles_index=synth_world["tiles_path"],
        interim_dir=synth_world["root"] / "interim",
        reports_dir=tmp_path / "reports",
        aoi_geojson=config_dir / "aoi.geojson",  # does not exist -> coverage check skipped
        manifest=tmp_path / "manifest.json",
    )


@pytest.fixture
def selected(cfg: dict, paths: Paths, synth_world: dict) -> pd.DataFrame:
    """tiles_split.csv + recheck_tiles.csv written for the synthetic world."""
    from geoqc.annotation.select_tiles import select_tiles

    select_tiles(cfg, paths, synth_world["osm_path"])
    return pd.read_csv(paths.tiles_split_csv, dtype={"tile_id": str})


@pytest.fixture
def drafted(cfg: dict, paths: Paths, synth_world: dict, selected: pd.DataFrame) -> dict:
    """draft.geojson for both splits from the synthetic secondary source."""
    from geoqc.annotation.pre_annotate import pre_annotate

    out = {}
    for split in ("val", "test"):
        out[split] = pre_annotate(cfg, paths, split, source_path=synth_world["secondary_path"])
    return out
