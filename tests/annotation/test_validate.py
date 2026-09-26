"""Tests for label validation and the G2 agreement metric (validate.py)."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.affinity import translate
from shapely.geometry import MultiPolygon, Polygon, box
from typer.testing import CliRunner

from geoqc.annotation.cli import app
from geoqc.annotation.common import Paths, write_geojson
from geoqc.annotation.validate import agreement_metrics, iou, match_pairs, validate_split
from tests.annotation.conftest import METRIC


@pytest.fixture
def accepted_labels(drafted: dict, paths: Paths) -> dict[str, gpd.GeoDataFrame]:
    """buildings.geojson для обоих сплитов: черновик, отмеченный как проверенный вручную."""
    out = {}
    for split in ("val", "test"):
        gdf = gpd.read_file(paths.split_dir(split) / "draft.geojson")
        gdf["verified"] = True
        gdf["source"] = "manual"
        gdf["annotator"] = "student"
        write_geojson(gdf, paths.split_dir(split) / "buildings.geojson")
        out[split] = gdf
    return out


def _run(cfg, paths, split, **kw):
    return validate_split(
        cfg, paths, split, report_path=paths.reports_dir / "annotation.json", **kw
    )


def _codes(report, split):
    return report["splits"][split]["errors_by_code"]


def test_accepted_labels_pass(cfg, paths, accepted_labels):
    """Корректная разметка принимается: статус accepted, код 0, без ошибок, предупреждение об отсутствии recheck."""
    report, code = _run(cfg, paths, "val")
    e = report["splits"]["val"]
    assert code == 0 and e["status"] == "accepted" and e["n_errors"] == 0
    assert e["stats"]["n_verified"] == e["stats"]["n_features"] == len(accepted_labels["val"])
    assert "recheck_missing" in e["warnings_by_code"]
    assert report["annotation"] == {"agreement_iou": None, "agreement_f1": None, "n_checked": 0}
    assert report["gates"]["G2"]["passed"] is False and report["gates"]["G2"]["note"]


def test_missing_file_rejected(cfg, paths, selected):
    """Отсутствующий buildings.geojson -> статус missing, код 1, отчёт всё равно записан."""
    report, code = _run(cfg, paths, "val")
    assert code == 1 and report["splits"]["val"]["status"] == "missing"
    assert (paths.reports_dir / "annotation.json").exists()


def test_unverified_feature_rejected(cfg, paths, accepted_labels):
    """Один объект с verified=false -> ошибка not_verified и ненулевой код."""
    gdf = accepted_labels["val"].copy()
    gdf.loc[gdf.index[0], "verified"] = False
    write_geojson(gdf, paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    assert code == 1 and _codes(report, "val") == {"not_verified": 1}


def test_verified_string_true_is_accepted(cfg, paths, accepted_labels):
    """QGIS может сохранить verified строкой 'true' или числом 1 — это принимается."""
    gdf = accepted_labels["val"].copy()
    gdf["verified"] = gdf["verified"].astype(object)
    gdf.loc[gdf.index[0], "verified"] = "true"
    gdf.loc[gdf.index[1], "verified"] = 1
    write_geojson(gdf, paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    assert code == 0, report["splits"]["val"]["errors"]


def test_invalid_geometry_rejected(cfg, paths, accepted_labels):
    """Самопересекающийся полигон («бабочка») -> ошибка invalid_geometry."""
    gdf = accepted_labels["val"].to_crs(METRIC)
    c = gdf.geometry.iloc[0].centroid
    bowtie = Polygon([(c.x, c.y), (c.x + 20, c.y + 20), (c.x + 20, c.y), (c.x, c.y + 20)])
    gdf.loc[gdf.index[0], "geometry"] = bowtie
    write_geojson(gdf, paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    assert code == 1 and "invalid_geometry" in _codes(report, "val")


def test_small_area_rejected(cfg, paths, accepted_labels):
    """Площадь меньше min_area_m2 -> ошибка min_area."""
    gdf = accepted_labels["val"].to_crs(METRIC)
    c = gdf.geometry.iloc[0].centroid
    gdf.loc[gdf.index[0], "geometry"] = MultiPolygon([box(c.x, c.y, c.x + 2, c.y + 2)])
    write_geojson(gdf, paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    assert code == 1 and _codes(report, "val") == {"min_area": 1}


def test_duplicate_rejected(cfg, paths, accepted_labels):
    """Дубликат объекта (наложение > 50 % меньшего) -> ошибка duplicate_overlap."""
    gdf = accepted_labels["val"]
    dup = pd.concat([gdf, gdf.iloc[[0]]], ignore_index=True)
    write_geojson(gpd.GeoDataFrame(dup, crs=gdf.crs), paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    assert code == 1 and _codes(report, "val") == {"duplicate_overlap": 1}


def test_small_overlap_is_warning(cfg, paths, accepted_labels):
    """Небольшое наложение соседних контуров (< 50 %) — предупреждение small_overlap, не ошибка."""
    gdf = accepted_labels["val"].to_crs(METRIC)
    g0 = gdf.geometry.iloc[0]
    minx, miny, maxx, maxy = g0.bounds
    # new 10x10 building overlapping the first one by 2x10 = 20 m² (< 50 % of 100 m²) on the same tile
    extra = gdf.iloc[[0]].copy()
    extra["geometry"] = [MultiPolygon([box(maxx - 2, miny, maxx + 8, miny + 10)])]
    gdf = pd.concat([gdf, extra], ignore_index=True)
    write_geojson(gpd.GeoDataFrame(gdf, crs=METRIC), paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    e = report["splits"]["val"]
    assert "small_overlap" in e["warnings_by_code"]
    assert "duplicate_overlap" not in e["errors_by_code"]


def test_wrong_tile_and_split_rejected(cfg, paths, accepted_labels, selected):
    """tile_id из другого сплита и неверный split -> ошибки unknown_tile / bad_split."""
    gdf = accepted_labels["val"].copy()
    test_tile = selected.loc[selected["split"] == "test", "tile_id"].iloc[0]
    gdf.loc[gdf.index[0], "tile_id"] = test_tile
    gdf.loc[gdf.index[1], "split"] = "test"
    write_geojson(gdf, paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    codes = _codes(report, "val")
    assert code == 1 and codes == {"unknown_tile": 1, "bad_split": 1}


def test_outside_tile_rejected(cfg, paths, accepted_labels, selected):
    """Контур, выходящий за рамку своего тайла больше допуска, -> ошибка outside_tile."""
    gdf = accepted_labels["val"].to_crs(METRIC)
    gdf.loc[gdf.index[0], "geometry"] = translate(
        gdf.geometry.iloc[0], 0, -60
    )  # buildings sit 40 m from the edge
    write_geojson(gdf, paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    assert code == 1 and "outside_tile" in _codes(report, "val")


def test_missing_property_rejected(cfg, paths, accepted_labels):
    """Отсутствие обязательного свойства (annotator) -> ошибка missing_property."""
    gdf = accepted_labels["val"].drop(columns=["annotator"])
    write_geojson(gdf, paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    assert code == 1 and _codes(report, "val") == {"missing_property": 1}


def test_tile_without_labels_is_warning(cfg, paths, accepted_labels):
    """Тайл сплита без единого объекта — предупреждение tile_without_labels (не блокирует приёмку)."""
    gdf = accepted_labels["val"]
    first_tile = gdf["tile_id"].iloc[0]
    write_geojson(gdf[gdf["tile_id"] != first_tile], paths.split_dir("val") / "buildings.geojson")
    report, code = _run(cfg, paths, "val")
    e = report["splits"]["val"]
    assert code == 0 and e["stats"]["tiles_without_labels"] == [first_tile]
    assert e["warnings_by_code"]["tile_without_labels"] == 1


# ------------------------------------------------------------------ G2 metric


def test_iou_squares(square, shifted_square):
    """IoU: одинаковые квадраты = 1, сдвинутый на половину стороны = 1/3, непересекающиеся = 0."""
    assert iou(square, square) == pytest.approx(1.0)
    assert iou(square, shifted_square) == pytest.approx(1 / 3)
    assert iou(square, box(20, 20, 30, 30)) == 0.0


def test_agreement_shifted_squares(square, shifted_square):
    """G2 на сдвинутых квадратах: средний IoU пар = 1/3, при пороге 0.5 пар TP нет -> F1 = 0."""
    m = agreement_metrics([square], [shifted_square], iou_match=0.5)
    assert m["n_pairs"] == 1 and m["mean_iou_pairs"] == pytest.approx(1 / 3)
    assert (m["tp"], m["fp"], m["fn"]) == (0, 1, 1) and m["f1"] == 0.0


def test_agreement_identical_and_missing(square):
    """Идентичная разметка: IoU 1, F1 1. Пропущенный объект: F1 = 2/3, IoU пар = 1."""
    other = box(50, 50, 60, 60)
    m = agreement_metrics([square, other], [square, other])
    assert m["mean_iou_pairs"] == pytest.approx(1.0) and m["f1"] == pytest.approx(1.0)
    m = agreement_metrics([square, other], [square])
    assert (m["tp"], m["fp"], m["fn"]) == (1, 0, 1) and m["f1"] == pytest.approx(2 / 3)
    m = agreement_metrics([], [])
    assert m["f1"] == 1.0 and m["mean_iou_pairs"] is None


def test_match_pairs_is_one_to_one(square):
    """Жадное сопоставление: один объект не может войти в две пары, берётся пара с большим IoU."""
    big = box(0, 0, 10, 10)
    pairs = match_pairs([square], [translate(big, 1, 0), translate(big, 5, 0)])
    assert len(pairs) == 1 and pairs[0][1] == 0 and pairs[0][2] > 0.8


def test_g2_end_to_end(cfg, paths, accepted_labels):
    """Recheck со сдвигом 0.5 м на recheck-тайлах val -> agreement_iou >= 0.8, G2 passed, отчёт по схеме."""
    rc = pd.read_csv(paths.recheck_tiles_csv, dtype=str)
    rc_val = rc.loc[rc["split"] == "val", "tile_id"].tolist()
    labels = accepted_labels["val"].to_crs(METRIC)
    recheck = labels[labels["tile_id"].isin(rc_val)].copy()
    recheck["geometry"] = [translate(g, 0.5, 0.5) for g in recheck.geometry]
    recheck["source"] = "manual"
    write_geojson(recheck, paths.split_dir("val") / "recheck.geojson")
    report, code = _run(cfg, paths, "val")
    assert code == 0
    agr = report["splits"]["val"]["agreement"]
    assert sorted(agr["recheck_tiles"]) == sorted(rc_val)
    assert agr["n_checked"] == len(rc_val)
    assert 0.8 <= agr["agreement_iou"] < 1.0 and agr["agreement_f1"] == 1.0
    assert set(agr["per_tile"]) == set(rc_val)
    assert report["annotation"]["agreement_iou"] == pytest.approx(agr["agreement_iou"])
    assert report["annotation"]["n_checked"] == len(rc_val)
    assert report["gates"]["G2"]["passed"] is True and report["gates"]["G2"]["target"] == 0.8


def test_g2_shifted_recheck_fails_gate(cfg, paths, accepted_labels):
    """Recheck со сдвигом 5 м поперёк зданий -> agreement_iou низкий, G2 не пройден, --strict даёт код 2."""
    rc = pd.read_csv(paths.recheck_tiles_csv, dtype=str)
    rc_val = rc.loc[rc["split"] == "val", "tile_id"].tolist()
    labels = accepted_labels["val"].to_crs(METRIC)
    recheck = labels[labels["tile_id"].isin(rc_val)].copy()
    recheck["geometry"] = [
        translate(g, 0, 5.0) for g in recheck.geometry
    ]  # depths are 8-30 m -> IoU <= 0.71
    write_geojson(recheck, paths.split_dir("val") / "recheck.geojson")
    report, code = _run(cfg, paths, "val")
    assert code == 0 and report["gates"]["G2"]["passed"] is False
    assert "g2_below_target" in report["splits"]["val"]["warnings_by_code"]
    _, strict_code = _run(cfg, paths, "val", strict=True)
    assert strict_code == 2


def test_report_merges_both_splits(cfg, paths, accepted_labels):
    """Отчёт reports/annotation.json накапливает оба сплита и агрегирует G2 по ним."""
    for split in ("val", "test"):
        rc = pd.read_csv(paths.recheck_tiles_csv, dtype=str)
        ids = rc.loc[rc["split"] == split, "tile_id"].tolist()
        lab = accepted_labels[split]
        write_geojson(lab[lab["tile_id"].isin(ids)], paths.split_dir(split) / "recheck.geojson")
        _run(cfg, paths, split)
    report = json.loads((paths.reports_dir / "annotation.json").read_text())
    assert set(report["splits"]) == {"val", "test"}
    n = sum(report["splits"][s]["agreement"]["n_checked"] for s in ("val", "test"))
    assert report["annotation"]["n_checked"] == n
    assert report["annotation"]["agreement_iou"] == pytest.approx(1.0)
    assert report["annotation"]["agreement_f1"] == pytest.approx(1.0)


def test_cli_validate_exit_codes(cli_env: Path, paths: Paths, accepted_labels):
    """CLI validate: код 0 и 'labels accepted' на корректной разметке, код 1 при verified=false."""
    base = [
        "validate",
        "--split",
        "test",
        "--labels-dir",
        str(paths.labels_dir),
        "--tiles-index",
        str(paths.tiles_index),
        "--report",
        str(paths.reports_dir / "annotation.json"),
    ]
    res = CliRunner().invoke(app, base)
    assert res.exit_code == 0, res.output
    assert "labels accepted" in res.output
    gdf = accepted_labels["test"].copy()
    gdf["verified"] = False
    write_geojson(gdf, paths.split_dir("test") / "buildings.geojson")
    res = CliRunner().invoke(app, base)
    assert res.exit_code == 1
    assert "not_verified" in res.output and "NOT accepted" in res.output
