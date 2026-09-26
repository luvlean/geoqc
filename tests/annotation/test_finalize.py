"""Tests for finalize.py: QGIS working files -> contract GeoJSON."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
from shapely.affinity import translate
from shapely.geometry import box
from typer.testing import CliRunner

from geoqc.annotation.cli import app
from geoqc.annotation.common import (
    LABEL_PROPERTIES,
    Paths,
    read_tiles,
    read_tiles_split,
    split_tiles,
)
from geoqc.annotation.finalize import finalize_split, normalise_labels
from geoqc.annotation.qgis_project import create_work_gpkg
from tests.annotation.conftest import METRIC


@pytest.fixture
def work(cfg, paths, drafted) -> tuple[Paths, gpd.GeoDataFrame]:
    """work.gpkg из черновика val с типичными «студенческими» огрехами."""
    sdir = paths.split_dir("val")
    create_work_gpkg(sdir / "work.gpkg", sdir / "draft.geojson")
    gdf = gpd.read_file(sdir / "work.gpkg", layer="buildings").to_crs(METRIC)
    gdf["verified"] = True
    tiles = split_tiles(
        read_tiles(paths.tiles_index), read_tiles_split(paths.tiles_split_csv), "val"
    ).to_crs(METRIC)
    t0 = tiles.set_index("tile_id").loc[gdf["tile_id"].iloc[0], "geometry"]
    minx, miny, maxx, maxy = t0.bounds
    # 1) a new hand-drawn building sticking 5 m out of the tile, no tile_id, source empty
    extra = gpd.GeoDataFrame(
        {
            "tile_id": [None],
            "split": [None],
            "source": [None],
            "verified": [True],
            "annotator": [None],
            "notes": [None],
        },
        geometry=[box(maxx - 10, miny + 50, maxx + 5, miny + 60)],
        crs=METRIC,
    )
    # 2) a sliver below min_area
    sliver = extra.copy()
    sliver["geometry"] = [box(minx + 60, miny + 60, minx + 62, miny + 63)]
    # 3) a wrong tile_id on an existing feature
    gdf.loc[gdf.index[1], "tile_id"] = "nope"
    all_ = pd.concat([gdf, extra, sliver], ignore_index=True)
    gpd.GeoDataFrame(all_, crs=METRIC).to_crs(4326).to_file(
        sdir / "work.gpkg", layer="buildings", driver="GPKG", mode="w"
    )
    return paths, gpd.GeoDataFrame(all_, crs=METRIC)


def test_finalize_cleans_and_writes_contract_file(cfg, work):
    """finalize: клипует по тайлу, назначает tile_id, убирает мелочь, нормализует свойства, пишет buildings.geojson."""
    paths, before = work
    rep = finalize_split(cfg, paths, "val", annotator="student")
    st = rep["layers"]["buildings"]
    assert st["n_input"] == len(before)
    assert st["n_output"] == len(before) - 1  # sliver dropped
    assert st["dropped_small"] == 1 and st["clipped"] >= 1 and st["tile_reassigned"] >= 2
    out = gpd.read_file(paths.split_dir("val") / "buildings.geojson")
    assert set(out.columns) - {"geometry"} == set(LABEL_PROPERTIES)
    assert (out["split"] == "val").all() and out["verified"].all()
    assert (
        set(out["source"]) <= {"manual", "pre_annotation"} and (out["annotator"] == "student").all()
    )
    tiles = (
        split_tiles(read_tiles(paths.tiles_index), read_tiles_split(paths.tiles_split_csv), "val")
        .to_crs(METRIC)
        .set_index("tile_id")
    )
    for tid, g in zip(out["tile_id"], out.to_crs(METRIC).geometry, strict=True):
        assert tiles.loc[tid, "geometry"].buffer(1e-6).contains(g)
    assert rep["layers"]["recheck"]["output"] is None  # empty recheck layer -> no recheck.geojson
    assert not (paths.split_dir("val") / "recheck.geojson").exists()


def test_finalize_then_validate_accepts(cfg, work):
    """После finalize разметка проходит validate без ошибок."""
    from geoqc.annotation.validate import validate_split

    paths, _ = work
    finalize_split(cfg, paths, "val", annotator="student")
    report, code = validate_split(
        cfg, paths, "val", report_path=paths.reports_dir / "annotation.json"
    )
    assert code == 0, report["splits"]["val"]["errors"]


def test_finalize_exports_recheck_layer(cfg, work):
    """Непустой слой recheck из work.gpkg экспортируется в recheck.geojson."""
    paths, before = work
    rc = pd.read_csv(paths.recheck_tiles_csv, dtype=str)
    tile = rc.loc[rc["split"] == "val", "tile_id"].iloc[0]
    sub = before[before["tile_id"] == tile].copy()
    sub["geometry"] = [translate(g, 0.3, 0.3) for g in sub.geometry]
    sub.to_crs(4326).to_file(
        paths.split_dir("val") / "work.gpkg", layer="recheck", driver="GPKG", mode="w"
    )
    rep = finalize_split(cfg, paths, "val")
    assert rep["layers"]["recheck"]["n_output"] == len(sub)
    out = gpd.read_file(paths.split_dir("val") / "recheck.geojson")
    assert set(out["tile_id"]) == {tile}


def test_finalize_geojson_in_place(cfg, paths, drafted):
    """Без work.gpkg finalize обрабатывает buildings.geojson на месте и оставляет .bak."""
    sdir = paths.split_dir("test")
    draft = gpd.read_file(sdir / "draft.geojson")
    draft["verified"] = "true"  # string, as QGIS may write it
    draft.to_file(sdir / "buildings.geojson", driver="GeoJSON")
    rep = finalize_split(cfg, paths, "test")
    assert rep["input"].endswith("buildings.geojson")
    assert (sdir / "buildings.geojson.bak").exists()
    out = gpd.read_file(sdir / "buildings.geojson")
    assert out["verified"].dtype == bool and out["verified"].all()
    assert rep["layers"]["buildings"]["n_output"] == len(draft)


def test_normalise_empty_frame(cfg, paths, selected):
    """Пустой вход даёт пустой кадр с полным набором свойств."""
    tiles = split_tiles(
        read_tiles(paths.tiles_index), read_tiles_split(paths.tiles_split_csv), "val"
    )
    out, st = normalise_labels(
        gpd.GeoDataFrame({"geometry": []}, crs=4326), tiles, "val", METRIC, 12.0
    )
    assert len(out) == 0 and set(LABEL_PROPERTIES) <= set(out.columns) and st["n_input"] == 0


def test_cli_finalize(cli_env, cfg, work):
    """CLI finalize печатает сводку по слоям и пишет buildings.geojson."""
    paths, _ = work
    res = CliRunner().invoke(
        app,
        [
            "finalize",
            "--split",
            "val",
            "--annotator",
            "student",
            "--labels-dir",
            str(paths.labels_dir),
            "--tiles-index",
            str(paths.tiles_index),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "buildings:" in res.output and (paths.split_dir("val") / "buildings.geojson").exists()
    assert {n for n, _ in pyogrio.list_layers(paths.split_dir("val") / "work.gpkg")} == {
        "buildings",
        "recheck",
    }
