"""Набор из footprints Microsoft: далеко от val/test, без пустых и почти пустых по снимку тайлов."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import rasterio
from shapely.geometry import box, mapping

from geoqc.cv.microsoft import excluded_tiles, export_microsoft_dataset

from .conftest import CRS_UTM, STEP_M, X0, Y0, write_tile


def _square(col: int, dx: float, size: float):
    """Square of ``size`` m inside tile ``0_<col>``, ``dx`` m from its left edge."""
    x = X0 + col * STEP_M + dx
    y = Y0 - 50
    return box(x, y - size, x + size, y)


def test_excluded_tiles_cover_labelled_and_neighbours():
    """val/test и их 8 соседей исключаются, остальные — нет."""
    df = pd.DataFrame({"tile_id": ["5_5", "9_9"], "split": ["val", "test"]})
    ex = excluded_tiles(df, radius=1)
    assert {"4_4", "5_6", "6_6", "8_8", "10_10"} <= ex
    assert len(ex) == 18 and "5_7" not in ex


def test_export_microsoft_dataset(tmp_path: Path):
    """0_0 (val) и сосед 0_1 исключены; 0_3, 0_4 со зданиями берутся; 0_2, 0_5 без зданий и 0_6 без снимка — нет."""
    tiles_dir = tmp_path / "tiles"
    tiles_dir.mkdir()
    rows = []
    for col in range(7):
        p = tiles_dir / f"0_{col}.tif"
        write_tile(p, 0, col, [], background=0 if col == 6 else 30)
        with rasterio.open(p) as src:
            rows.append(
                {
                    "tile_id": f"0_{col}",
                    "row": 0,
                    "col": col,
                    "path": str(p),
                    "geometry": box(*src.bounds),
                }
            )
    index = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS_UTM).to_crs("EPSG:4326")
    index.to_file(tiles_dir / "tiles.gpkg", driver="GPKG", layer="tiles")
    split_csv = tmp_path / "tiles_split.csv"
    pd.DataFrame(
        [{"tile_id": "0_0", "split": "val", "stratum": "mixed", "row": 0, "col": 0}]
    ).to_csv(split_csv, index=False)
    # Mid-tile positions: the 32 m overlap strips at tile edges stay empty.
    geoms = [
        _square(0, 100, 20),  # val tile
        _square(1, 100, 20),  # neighbour of val
        _square(3, 100, 20),  # far: kept
        _square(3, 180, 3),  # far but 9 m2: dropped
        _square(4, 100, 30),  # far: kept
        _square(6, 100, 20),  # nodata tile
    ]
    fp = tmp_path / "footprints.geojson"
    feats = gpd.GeoSeries(geoms, crs=CRS_UTM).to_crs("EPSG:4326")
    fp.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {}, "geometry": mapping(g)} for g in feats
                ],
            }
        ),
        encoding="utf-8",
    )
    cv_cfg = {
        "seed": 42,
        "microsoft": {"holdout_fraction": 0.5, "min_area_m2": 12, "min_valid_fraction": 0.9},
    }
    out = tmp_path / "ms"
    s = export_microsoft_dataset(
        cv_cfg,
        footprints_path=fp,
        split_csv=split_csv,
        tiles_dir=tiles_dir,
        index_path=tiles_dir / "tiles.gpkg",
        out_dir=out,
    )
    assert (s.n_excluded_near_labels, s.n_candidates) == (2, 5)
    assert (s.n_skipped_empty, s.n_skipped_nodata) == (2, 1)
    assert (s.n_train, s.n_holdout) == (1, 1)
    assert s.n_instances_train + s.n_instances_holdout == 2
    written = sorted(p.stem for p in (out / "images").rglob("*.png"))
    assert written == ["0_3", "0_4"]
    assert "images/holdout" in (out / "data.yaml").read_text()
    assert json.loads((out / "meta.json").read_text())["license"].startswith("ODbL")
