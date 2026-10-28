"""Полный predict на синтетических тайлах с мок-моделью: GeoJSON, детерминизм, CLI, БД."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
from typer.testing import CliRunner

from geoqc.cv.pipeline import (
    default_detections_path,
    load_to_db,
    read_detections,
    run_predict,
    summarize,
    write_detections,
)
from geoqc.cv.predictor import MockPredictor
from geoqc.cv.tiles import filter_by_split, load_tile_index, read_tile

EXPECTED_AREAS = sorted([900.0, 400.0, 576.0])  # squares A, B, C in m²


def test_tile_index_and_split_filter(synthetic_aoi: dict):
    """Индекс читается из tiles.gpkg, фильтр по сплиту даёт 2 val-тайла и 1 test-тайл."""
    index = load_tile_index(synthetic_aoi["tiles_dir"], synthetic_aoi["index"])
    assert list(index["tile_id"]) == ["0_0", "0_1", "1_0"]
    assert index.crs.to_epsg() == 4326
    assert len(filter_by_split(index, "val", synthetic_aoi["split_csv"])) == 2
    assert len(filter_by_split(index, "test", synthetic_aoi["split_csv"])) == 1
    assert len(filter_by_split(index, "all")) == 3
    assert (
        len(filter_by_split(index, "train", synthetic_aoi["split_csv"])) == 0
    )  # D-021 split, none here
    with pytest.raises(ValueError):
        filter_by_split(index, "holdout")
    tile = read_tile(index["path"].iloc[0], "0_0")
    assert tile.image.shape == (512, 512, 3) and tile.image.dtype.name == "uint8"
    assert not tile.nodata_mask.any()


def test_index_fallback_from_files(synthetic_aoi: dict):
    """Без tiles.gpkg индекс восстанавливается из имён файлов {row}_{col}.tif."""
    synthetic_aoi["index"].unlink()
    index = load_tile_index(synthetic_aoi["tiles_dir"])
    assert list(index["tile_id"]) == ["0_0", "0_1", "1_0"]
    assert index.crs.to_epsg() == 4326


def test_full_predict_on_two_synthetic_tiles(synthetic_aoi: dict):
    """predict на 2 val-тайлах: 3 здания (склейка A на перекрытии, фильтр пятна), контракт §3.6."""
    res = run_predict(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        split="val",
        predictor=MockPredictor(min_component_px=1),
        keep_raw=True,
    )
    gdf = res.detections
    assert res.n_tiles == 2 and res.n_masks == 5  # A(x2), B, C, speck
    assert len(gdf) == 3
    assert gdf.crs.to_epsg() == 4326
    assert set(gdf.columns) == {"geometry", "score", "model_version", "tile_id"}
    assert gdf.geom_type.unique().tolist() == ["MultiPolygon"]
    assert (gdf["model_version"] == "mock-00000000").all()
    areas = sorted(round(a, 3) for a in gdf.to_crs(synthetic_aoi["crs"]).area)
    assert areas == pytest.approx(EXPECTED_AREAS, abs=1.0)
    # square C is dimmer (220) -> lower score than A/B
    assert gdf["score"].min() < gdf["score"].max() <= 1.0
    s = summarize(res)
    assert s["n_detections"] == 3 and s["split"] == "val"


def test_predict_is_deterministic(synthetic_aoi: dict):
    """Два запуска predict с одинаковым seed дают побайтно одинаковый GeoJSON."""
    out1 = synthetic_aoi["data"] / "interim" / "d1.geojson"
    out2 = synthetic_aoi["data"] / "interim" / "d2.geojson"
    for out in (out1, out2):
        res = run_predict(
            synthetic_aoi["cv"],
            synthetic_aoi["pipeline"],
            split="all",
            predictor=MockPredictor(min_component_px=1),
        )
        write_detections(res.detections, out)
    assert out1.read_bytes() == out2.read_bytes()
    back = read_detections(out1)
    assert len(back) == 3 and back.crs.to_epsg() == 4326


def test_predict_test_split_has_no_buildings_and_writes_empty_collection(synthetic_aoi: dict):
    """Тёмный тайл сплита test -> 0 детекций, файл всё равно валидный FeatureCollection."""
    res = run_predict(
        synthetic_aoi["cv"], synthetic_aoi["pipeline"], split="test", predictor=MockPredictor()
    )
    assert res.n_detections == 0
    out = write_detections(res.detections, synthetic_aoi["data"] / "interim" / "empty.geojson")
    assert json.loads(out.read_text())["features"] == []
    assert len(read_detections(out)) == 0


def test_limit_restricts_tiles(synthetic_aoi: dict):
    """--limit 1 обрабатывает только первый тайл (0_0): здания A, B, без C."""
    res = run_predict(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        split="all",
        limit=1,
        predictor=MockPredictor(min_component_px=1),
    )
    assert res.n_tiles == 1 and res.n_detections == 2


def test_cli_predict_with_mock_backend(env_config: dict):
    """CLI `geoqc cv predict --split val --no-progress` через конфиг с backend=mock пишет detections_val.geojson."""
    from geoqc.cli import app

    runner = CliRunner()
    out = env_config["data"] / "interim" / "cli.geojson"
    result = runner.invoke(
        app, ["cv", "predict", "--split", "val", "--no-progress", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["n_detections"] == 3 and payload["backend"] == "mock"
    assert out.exists()
    assert (
        default_detections_path("val", env_config["data"] / "interim").name
        == "detections_val.geojson"
    )
    assert default_detections_path("all").name == "detections.geojson"


@pytest.mark.db
def test_load_to_db_replaces_previous_detections(synthetic_aoi: dict, db_conn):
    """--to-db: прежние детекции любых версий и очередь удаляются, геометрия MultiPolygon 4326."""
    res = run_predict(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        split="val",
        predictor=MockPredictor(min_component_px=1),
    )
    load_to_db(res.detections, "older-00000000", conn=db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO discrepancies (class, confidence, geom, model_version) "
            "VALUES ('missing_on_image', 0.5, ST_Multi(ST_Buffer(ST_GeomFromText('POINT(30 60)', 4326), 0.0001)), 'older-00000000')"
        )
    n1 = load_to_db(res.detections, res.model_version, conn=db_conn)
    n2 = load_to_db(res.detections, res.model_version, conn=db_conn)
    assert n1 == n2 == 3
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM buildings_det WHERE model_version = 'older-00000000'"
        )
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM discrepancies")
        assert cur.fetchone()["n"] == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n, min(ST_SRID(geom)) AS srid, min(GeometryType(geom)) AS gt FROM buildings_det WHERE model_version = %s",
            (res.model_version,),
        )
        row = cur.fetchone()
    assert row["n"] == 3 and row["srid"] == 4326 and row["gt"] == "MULTIPOLYGON"
    n3 = load_to_db(res.detections, res.model_version, conn=db_conn, replace=False)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM buildings_det WHERE model_version = %s", (res.model_version,)
        )
        assert cur.fetchone()["n"] == 3 + n3


def test_write_detections_reprojects_if_needed(synthetic_aoi: dict, tmp_path: Path):
    """write_detections приводит GeoDataFrame в метрической CRS к EPSG:4326."""
    from shapely.geometry import MultiPolygon, box

    gdf = gpd.GeoDataFrame(
        {"score": [0.5], "model_version": ["m-1"], "tile_id": ["0_0"]},
        geometry=[MultiPolygon([box(500000, 5999000, 500010, 5999010)])],
        crs=synthetic_aoi["crs"],
    )
    out = write_detections(gdf, tmp_path / "x.geojson")
    back = read_detections(out)
    assert back.crs.to_epsg() == 4326 and abs(back.geometry.iloc[0].centroid.y - 54.1) < 0.5
