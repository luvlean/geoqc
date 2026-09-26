"""Tests for pre-annotation (pre_annotate.py)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
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
from geoqc.annotation.pre_annotate import clip_to_tiles, pre_annotate, resolve_source
from tests.annotation.conftest import METRIC


def test_draft_is_clipped_to_split_tiles(drafted: dict, paths: Paths, cfg: dict):
    """Черновик содержит только тайлы сплита, каждая геометрия лежит внутри своего тайла и имеет верный tile_id."""
    tiles = read_tiles(paths.tiles_index)
    split_df = read_tiles_split(paths.tiles_split_csv)
    for split in ("val", "test"):
        draft = gpd.read_file(paths.split_dir(split) / "draft.geojson")
        tiles_s = split_tiles(tiles, split_df, split).to_crs(METRIC).set_index("tile_id")
        assert len(draft) > 0
        assert set(draft["tile_id"]) <= set(tiles_s.index)
        draft_m = draft.to_crs(METRIC)
        for tid, geom in zip(draft_m["tile_id"], draft_m.geometry, strict=True):
            assert tiles_s.loc[tid, "geometry"].buffer(1e-6).contains(geom), tid
        assert drafted[split]["n_draft_features"] == len(draft)


def test_draft_properties_follow_contract(drafted: dict, paths: Paths):
    """Свойства черновика: ровно набор §3.5, source=pre_annotation, verified=false, split верный, MultiPolygon."""
    draft = gpd.read_file(paths.split_dir("val") / "draft.geojson")
    assert set(draft.columns) - {"geometry"} == set(LABEL_PROPERTIES)
    assert (draft["source"] == "pre_annotation").all()
    assert draft["verified"].dtype == bool and not draft["verified"].any()
    assert (draft["split"] == "val").all()
    assert set(draft.geometry.geom_type) == {"MultiPolygon"}
    assert draft.crs.to_epsg() == 4326


def test_clip_drops_slivers_and_splits_by_tile():
    """Полигон на границе двух тайлов режется на две части; кусок меньше min_area отбрасывается."""
    tiles = gpd.GeoDataFrame(
        {"tile_id": ["a", "b"], "geometry": [box(0, 0, 100, 100), box(100, 0, 200, 100)]},
        crs=METRIC,
    )
    src = gpd.GeoDataFrame(
        {"geometry": [box(90, 10, 130, 30), box(98, 50, 101, 60), box(150, 50, 160, 60)]},
        crs=METRIC,
    )
    out = clip_to_tiles(src, tiles, METRIC, min_area_m2=12.0).to_crs(METRIC)
    pieces = sorted(
        (tid, round(g.area, 3)) for tid, g in zip(out["tile_id"], out.geometry, strict=True)
    )
    # 40x20 straddles: 10x20=200 in a, 30x20=600 in b; 3x10 sliver: 2x10=20 in a (kept), 1x10=10 in b (dropped); 10x10 in b
    assert pieces == [("a", 20.0), ("a", 200.0), ("b", 100.0), ("b", 600.0)]


def test_clip_empty_source_returns_empty_frame():
    """Пустой источник даёт пустой GeoDataFrame с колонкой tile_id и CRS 4326."""
    tiles = gpd.GeoDataFrame({"tile_id": ["a"], "geometry": [box(0, 0, 100, 100)]}, crs=METRIC)
    out = clip_to_tiles(gpd.GeoDataFrame({"geometry": []}, crs=METRIC), tiles, METRIC, 12.0)
    assert len(out) == 0 and "tile_id" in out.columns and out.crs.to_epsg() == 4326


def test_resolve_source_rules(cfg: dict, tmp_path: Path):
    """OSM как источник запрещён; отсутствующие файлы -> FileNotFoundError; явный путь имеет приоритет."""
    with pytest.raises(ValueError):
        resolve_source(cfg, "osm", None)
    with pytest.raises(FileNotFoundError):
        resolve_source(cfg, None, tmp_path / "missing.geojson")
    cfg["annotation"]["pre_annotation"]["secondary_path"] = str(tmp_path / "no_secondary.geojson")
    cfg["annotation"]["pre_annotation"]["detections_path"] = str(tmp_path / "no_detections.geojson")
    with pytest.raises(FileNotFoundError):
        resolve_source(cfg, None, None)
    p = tmp_path / "custom.geojson"
    p.write_text('{"type":"FeatureCollection","features":[]}')
    assert resolve_source(cfg, None, p) == ("custom", p)


def test_pre_annotate_refuses_overwrite(cfg: dict, paths: Paths, synth_world: dict, drafted: dict):
    """Повторный запуск без --force не перезаписывает draft.geojson; с force — перезаписывает."""
    with pytest.raises(FileExistsError):
        pre_annotate(cfg, paths, "val", source_path=synth_world["secondary_path"])
    rep = pre_annotate(cfg, paths, "val", source_path=synth_world["secondary_path"], force=True)
    assert rep["n_draft_features"] == drafted["val"]["n_draft_features"]


def test_pre_annotate_from_config_priority(
    cfg: dict, paths: Paths, synth_world: dict, selected: pd.DataFrame
):
    """Без --source берётся первый существующий источник из source_priority (secondary)."""
    cfg["annotation"]["pre_annotation"]["secondary_path"] = str(synth_world["secondary_path"])
    rep = pre_annotate(cfg, paths, "test")
    assert rep["source"] == "secondary"
    assert rep["n_draft_features"] > 0


def test_cli_pre_annotate(
    cli_env: Path, cfg: dict, paths: Paths, synth_world: dict, selected: pd.DataFrame
):
    """CLI pre-annotate создаёт draft.geojson и печатает сводку; повторный вызов без --force -> код 2."""
    args = [
        "pre-annotate",
        "--split",
        "val",
        "--source-path",
        str(synth_world["secondary_path"]),
        "--labels-dir",
        str(paths.labels_dir),
        "--tiles-index",
        str(paths.tiles_index),
    ]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0, res.output
    assert (paths.split_dir("val") / "draft.geojson").exists()
    assert "draft buildings" in res.output
    res2 = CliRunner().invoke(app, args)
    assert res2.exit_code == 2
