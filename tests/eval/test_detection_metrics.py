"""Чистые метрики детекции: IoU, 1:1 сопоставление, P/R/F1, mean IoU, оценка сплита."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Polygon, box

from geoqc.eval.detection_metrics import (
    MatchResult,
    area_weighted_prf,
    detection_summary,
    evaluate_geodataframes,
    evaluate_split,
    iou,
    iou_matrix,
    match_one_to_one,
    mean_iou,
    precision_recall_f1,
    val_training_note,
)


def test_iou_identical_squares(square):
    """IoU одинаковых квадратов равен 1."""
    assert iou(square, square) == pytest.approx(1.0)


def test_iou_shifted_square_is_one_third(square, shifted_square):
    """Квадрат 10x10, сдвинутый на 5 м: пересечение 50, объединение 150 -> IoU = 1/3."""
    assert iou(square, shifted_square) == pytest.approx(1.0 / 3.0)


def test_iou_disjoint_and_degenerate(square):
    """Непересекающиеся, пустые и None геометрии дают IoU = 0."""
    assert iou(square, box(20, 20, 30, 30)) == 0.0
    assert iou(square, box(10, 0, 20, 10)) == 0.0  # touching edge only
    assert iou(square, Polygon()) == 0.0
    assert iou(None, square) == 0.0


def test_iou_matrix_shape_and_values(square, shifted_square):
    """Матрица IoU имеет размер |GT| x |pred| и нули для непересекающихся пар."""
    mat = iou_matrix([square, box(100, 100, 110, 110)], [shifted_square, square])
    assert mat.shape == (2, 2)
    assert mat[0, 0] == pytest.approx(1.0 / 3.0)
    assert mat[0, 1] == pytest.approx(1.0)
    assert mat[1].tolist() == [0.0, 0.0]
    assert iou_matrix([], [square]).shape == (0, 1)


def test_greedy_vs_hungarian_on_conflict():
    """Конфликт: жадный берёт лучшую пару (A,P1) и теряет B; венгерский находит две пары."""
    a, b = box(0, 0, 10, 10), box(10, 0, 20, 10)
    p1, p2 = box(2, 0, 16, 10), box(-4, 0, 6, 10)
    mat = iou_matrix([a, b], [p1, p2])
    assert mat[0, 0] == pytest.approx(0.5)  # A-P1
    assert mat[1, 0] == pytest.approx(1.0 / 3.0)  # B-P1
    assert mat[0, 1] == pytest.approx(60 / 140)  # A-P2
    greedy = match_one_to_one([a, b], [p1, p2], iou_thr=0.3, method="greedy")
    hung = match_one_to_one([a, b], [p1, p2], iou_thr=0.3, method="hungarian")
    assert greedy.tp == 1 and greedy.pairs[0][:2] == (0, 0)
    assert greedy.unmatched_gt == [1] and greedy.unmatched_pred == [1]
    assert hung.tp == 2 and hung.fp == 0 and hung.fn == 0
    assert {(g, p) for g, p, _ in hung.pairs} == {(0, 1), (1, 0)}


def test_greedy_and_hungarian_agree_without_conflict(square, shifted_square):
    """Без конфликтов оба метода дают одинаковые пары и не засчитывают пары ниже порога."""
    gts = [square, box(100, 100, 120, 120)]
    preds = [box(101, 100, 121, 120), shifted_square]  # IoU 19*20/(400+400-380)=0.905 and 1/3
    for method in ("greedy", "hungarian"):
        res = match_one_to_one(gts, preds, iou_thr=0.5, method=method)
        assert res.pairs[0][:2] == (1, 0)
        assert res.tp == 1 and res.fp == 1 and res.fn == 1
        assert res.method == method


def test_match_one_to_one_empty_inputs_and_bad_method(square):
    """Пустые входы: все GT -> FN, все pred -> FP; неизвестный метод -> ValueError."""
    res = match_one_to_one([square], [])
    assert (res.tp, res.fp, res.fn) == (0, 0, 1)
    res = match_one_to_one([], [square])
    assert (res.tp, res.fp, res.fn) == (0, 1, 0)
    with pytest.raises(ValueError):
        match_one_to_one([square], [square], method="magic")


@pytest.mark.parametrize(
    ("tp", "fp", "fn", "expected"),
    [
        (0, 0, 5, (0.0, 0.0, 0.0)),  # no predictions
        (0, 4, 0, (0.0, 0.0, 0.0)),  # no ground truth
        (0, 0, 0, (0.0, 0.0, 0.0)),  # nothing at all
        (5, 0, 0, (1.0, 1.0, 1.0)),  # perfect
        (3, 1, 2, (0.75, 0.6, 2 * 0.75 * 0.6 / 1.35)),
    ],
)
def test_precision_recall_f1_edge_cases(tp, fp, fn, expected):
    """P/R/F1: нулевые знаменатели дают 0.0, обычный случай — по формулам."""
    assert precision_recall_f1(tp, fp, fn) == pytest.approx(expected)


def test_mean_iou_over_pairs_and_floats():
    """mean IoU принимает тройки (gt, pred, iou) или числа; пусто -> 0.0."""
    assert mean_iou([(0, 0, 0.5), (1, 1, 1.0)]) == pytest.approx(0.75)
    assert mean_iou([0.2, 0.4]) == pytest.approx(0.3)
    assert mean_iou([]) == 0.0
    summary = detection_summary(
        MatchResult(pairs=[(0, 0, 0.8)], unmatched_gt=[1], unmatched_pred=[])
    )
    assert summary == {
        "tp": 1,
        "fp": 0,
        "fn": 1,
        "precision": 1.0,
        "recall": 0.5,
        "f1": pytest.approx(2 / 3),
        "mean_iou_matched": 0.8,
    }


def test_evaluate_geodataframes_clips_and_assigns_tiles(synthetic_env):
    """Синтетический сплит val: TP=4, FP=1 (F), FN=1 (E); детекции вне тайлов отброшены, FP отнесён к тайлу по геометрии."""
    import geopandas as gpd

    from geoqc.eval.detection_metrics import load_split_tiles

    p = synthetic_env.paths
    tile_ids, strata, tiles = load_split_tiles(p, "val")
    crs = "EPSG:32637"
    gt = gpd.read_file(p.labels("val")).to_crs(crs)
    det = gpd.read_file(p.detections).to_crs(crs)
    ev = evaluate_geodataframes(gt, det, tiles.to_crs(crs), tile_ids, strata, metric_crs=crs)
    s = ev.summary()
    assert (s["tp"], s["fp"], s["fn"]) == (4, 1, 1)
    assert (
        s["precision"] == pytest.approx(0.8)
        and s["recall"] == pytest.approx(0.8)
        and s["f1"] == pytest.approx(0.8)
    )
    assert s["mean_iou_matched"] == pytest.approx((360 / 440 + 1.0 + 0.64 + 1.0) / 4)
    assert len(ev.det) == 5  # H' and Z dropped by clipping
    per_tile = ev.per_tile().set_index("tile_id")
    assert per_tile.loc["0_0", ["n_gt", "n_det", "tp", "fp", "fn"]].tolist() == [3, 4, 3, 1, 0]
    assert per_tile.loc["0_1", ["n_gt", "n_det", "tp", "fp", "fn"]].tolist() == [2, 1, 1, 0, 1]
    assert ev.per_stratum()["low_rise_private"]["recall"] == pytest.approx(0.5)
    assert set(ev.det["tile_id"]) == {"0_0", "0_1"}  # F reassigned from "9_9" to 0_0


def test_evaluate_geodataframes_without_tile_polygons_filters_by_tile_id(synthetic_env):
    """Без полигонов тайлов детекции фильтруются по своему tile_id (F с tile_id 9_9 отбрасывается) и пишется note."""
    import geopandas as gpd

    p = synthetic_env.paths
    crs = "EPSG:32637"
    gt = gpd.read_file(p.labels("val")).to_crs(crs)
    det = gpd.read_file(p.detections).to_crs(crs)
    ev = evaluate_geodataframes(
        gt, det, None, ["0_0", "0_1"], {"0_0": "a", "0_1": "b"}, metric_crs=crs
    )
    assert (ev.match.tp, ev.match.fp, ev.match.fn) == (4, 0, 1)
    assert any("not clipped" in n for n in ev.notes)


def test_evaluate_split_reads_files_and_flags_verification(synthetic_env):
    """evaluate_split читает файлы контракта, определяет verified, model_version и режет детекции по score."""
    ev = evaluate_split("val", synthetic_env.paths, synthetic_env.cfg)
    assert ev.labels_verified is True
    assert ev.model_version == "synthetic-00000000"
    assert ev.metric_crs == "EPSG:32637" and "pipeline.yaml" in ev.metric_crs_source
    report = ev.to_report("now", "abc", smoke=False)
    assert report["is_metric"] is True and report["n_tiles"] == 2 and report["n_gt"] == 5
    draft = evaluate_split(
        "val", synthetic_env.paths, synthetic_env.cfg, labels_path=synthetic_env.draft_val
    )
    assert draft.labels_verified is False
    strict = evaluate_split("val", synthetic_env.paths, synthetic_env.cfg, min_score=0.75)
    assert strict.match.tp == 3 and strict.conf_threshold == 0.75  # C' (0.7) and F (0.6) removed
    test = evaluate_split("test", synthetic_env.paths, synthetic_env.cfg)
    assert (test.match.tp, test.match.fp, test.match.fn) == (1, 0, 0)


def test_evaluate_split_missing_inputs_raise(synthetic_env):
    """Отсутствие детекций -> EvalInputError с путём файла."""
    from geoqc.eval.common import EvalInputError

    synthetic_env.paths.detections.unlink()
    with pytest.raises(EvalInputError, match="detections not found"):
        evaluate_split("val", synthetic_env.paths, synthetic_env.cfg)


def test_per_tile_dataframe_columns_on_empty_result():
    """Пустая оценка даёт таблицу per_tile с полным набором столбцов."""
    import geopandas as gpd

    empty = gpd.GeoDataFrame({"tile_id": pd.Series(dtype=str)}, geometry=[], crs="EPSG:32637")
    ev = evaluate_geodataframes(
        empty, empty.copy(), None, ["0_0"], {"0_0": "mixed"}, metric_crs="EPSG:32637"
    )
    table = ev.per_tile()
    assert list(table.columns)[:4] == ["tile_id", "stratum", "n_gt", "n_det"]
    assert table.iloc[0].tolist()[:7] == ["0_0", "mixed", 0, 0, 0, 0, 0]
    assert ev.summary()["f1"] == 0.0
    assert np.all(ev.iou_matrix.shape == (0, 0))


def test_val_training_note_counts_train_tiles(tmp_path):
    """Тайлы val из train-части датасета дообучения помечаются: метрики val оптимистичны."""
    pd.DataFrame(
        {"tile_id": ["1_1", "1_2", "2_2"], "subset": ["train", "train", "holdout"]}
    ).to_csv(tmp_path / "split.csv", index=False)
    cfg = {"cv": {"dataset": {"out_dir": str(tmp_path)}}}
    notes = val_training_note(cfg, ["1_1", "1_2", "2_2", "9_9"])
    assert len(notes) == 1 and notes[0].startswith("2 of 4 val tiles")
    assert (
        val_training_note({"cv": {"dataset": {"out_dir": str(tmp_path / "nope")}}}, ["1_1"]) == []
    )


def test_area_weighted_prf_weighs_large_buildings_more():
    """Взвешенный F1 (D-023): пропущенный дом 1000 м² весит больше, чем пропущенный сарай 20 м²."""
    gts = [box(0, 0, 40, 25), box(100, 0, 104, 5)]  # 1000 m2 block, 20 m2 shed
    found_block = match_one_to_one(gts, [box(0, 0, 40, 25)], 0.5)
    found_shed = match_one_to_one(gts, [box(100, 0, 104, 5)], 0.5)
    gt_areas = [g.area for g in gts]
    pb, rb, fb = area_weighted_prf(found_block, gt_areas, [1000.0])
    ps, rs, fs = area_weighted_prf(found_shed, gt_areas, [20.0])
    assert (pb, ps) == (1.0, 1.0)
    assert rb == pytest.approx(1000 / 1020) and rs == pytest.approx(20 / 1020)
    assert fb > 0.98 > 0.05 > fs
    # plain F1 cannot tell the two cases apart
    assert detection_summary(found_block)["f1"] == detection_summary(found_shed)["f1"]
    s = detection_summary(found_block, gt_areas, [1000.0])
    assert s["f1_area"] == pytest.approx(fb)


def test_area_weighted_prf_empty():
    """Без зданий и детекций взвешенные метрики равны нулю, деления на ноль нет."""
    assert area_weighted_prf([], [], []) == (0.0, 0.0, 0.0)
