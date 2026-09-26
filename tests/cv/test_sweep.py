"""threshold-sweep: локальные метрики §4, выбор порога на синтетике, отчёт и запись в конфиг."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from shapely.geometry import box

from geoqc.cv.predictor import MockPredictor
from geoqc.cv.sweep import (
    evaluate,
    iou,
    match_one_to_one,
    pick_best,
    precision_recall_f1,
    run_sweep,
)


def test_iou_and_prf_formulas(square, shifted_square):
    """IoU(квадрат, сдвиг на 5) = 1/3; P/R/F1 по формулам §4 и крайние случаи."""
    assert iou(square, square) == pytest.approx(1.0)
    assert iou(square, shifted_square) == pytest.approx(1 / 3)
    assert iou(square, box(50, 50, 60, 60)) == 0.0
    assert precision_recall_f1(3, 1, 1) == pytest.approx((0.75, 0.75, 0.75))
    assert precision_recall_f1(0, 0, 0) == (0.0, 0.0, 0.0)
    assert precision_recall_f1(0, 5, 0) == (0.0, 0.0, 0.0)


def test_greedy_one_to_one_matching():
    """Жадное сопоставление: один GT не может быть засчитан двум детекциям; IoU < 0.5 не пара."""
    gts = [box(0, 0, 10, 10), box(20, 0, 30, 10)]
    preds = [box(0, 0, 10, 10), box(1, 1, 11, 11), box(25, 0, 35, 10), box(100, 100, 101, 101)]
    pairs = match_one_to_one(gts, preds, 0.5)
    assert len(pairs) == 1  # gt0 <-> pred0 (IoU 1); pred1 loses; pred2 IoU=1/3 < 0.5
    assert pairs[0][:2] == (0, 0)
    m = evaluate(gts, preds, 0.5)
    assert (m["tp"], m["fp"], m["fn"]) == (1, 3, 1)
    assert m["precision"] == 0.25 and m["recall"] == 0.5 and m["mean_iou_matched"] == 1.0
    assert m["metrics_impl"] in {"local", "geoqc.eval.detection_metrics"}
    assert evaluate([], [], 0.5)["f1"] == 0.0
    assert match_one_to_one([], preds) == []


def test_pick_best_ties_prefer_precision_then_higher_conf():
    """При равном F1 выбирается строка с большей precision, затем с большим порогом."""
    rows = [
        {"conf": 0.1, "f1": 0.8, "precision": 0.7},
        {"conf": 0.5, "f1": 0.8, "precision": 0.9},
        {"conf": 0.7, "f1": 0.8, "precision": 0.9},
    ]
    assert pick_best(rows)["conf"] == 0.7
    with pytest.raises(ValueError):
        pick_best([])


def test_run_sweep_on_synthetic_val(synthetic_aoi: dict):
    """Sweep на val: при порогах <= 0.9 F1=1 (3 здания), при >= 0.95 тусклое здание C (score 0.93) теряется -> выбор 0.9."""
    cfg_path = synthetic_aoi["config_dir"] / "cv.yaml"
    report = run_sweep(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        split="val",
        labels_path=synthetic_aoi["labels"],
        split_csv=synthetic_aoi["split_csv"],
        predictor=MockPredictor(min_component_px=1),
        config_path=cfg_path,
    )
    by_conf = {r["conf"]: r for r in report["thresholds"]}
    assert report["n_gt"] == 3 and report["n_tiles"] == 2
    assert by_conf[0.5]["f1"] == 1.0 and by_conf[0.9]["f1"] == 1.0
    assert by_conf[0.95]["tp"] == 2 and by_conf[0.95]["fn"] == 1 and by_conf[0.95]["f1"] == 0.8
    assert by_conf[0.99]["tp"] == 2  # C has score 0.5 + 0.5 * 220 / 255 = 0.93
    assert report["best"]["conf"] == 0.9  # tie with 0.5 -> higher threshold wins
    saved = json.loads(Path(report["report_path"]).read_text())
    assert saved["split"] == "val" and saved["iou_threshold"] == 0.5
    assert yaml.safe_load(cfg_path.read_text())["conf_threshold"] == 0.9


def test_run_sweep_from_detections_file_and_holdout(synthetic_aoi: dict):
    """--detections переиспользует готовый GeoJSON; --holdout сужает тайлы до holdout из export-dataset."""
    from geoqc.cv.dataset import export_dataset, read_holdout_tile_ids
    from geoqc.cv.pipeline import run_predict, write_detections

    res = run_predict(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        split="val",
        predictor=MockPredictor(min_component_px=1),
    )
    det_path = write_detections(res.detections, synthetic_aoi["data"] / "interim" / "dets.geojson")
    report = run_sweep(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        detections_path=det_path,
        labels_path=synthetic_aoi["labels"],
        split_csv=synthetic_aoi["split_csv"],
        update_config=False,
    )
    assert report["detections_source"].endswith("dets.geojson")
    assert {r["conf"]: r["f1"] for r in report["thresholds"]}[0.5] == 1.0

    ds = synthetic_aoi["data"] / "interim" / "dataset"
    export_dataset(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        labels_path=synthetic_aoi["labels"],
        split_csv=synthetic_aoi["split_csv"],
        out_dir=ds,
    )
    hold = read_holdout_tile_ids(ds)
    report = run_sweep(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        holdout_only=True,
        dataset_dir=ds,
        labels_path=synthetic_aoi["labels"],
        split_csv=synthetic_aoi["split_csv"],
        predictor=MockPredictor(min_component_px=1),
        update_config=False,
    )
    assert report["n_tiles"] == 1 and report["subset"] == "val/holdout"
    assert report["n_gt"] == (
        2 if hold == ["0_0"] else 2
    )  # A is on the overlap, so 2 GT touch either tile


def test_sweep_refuses_other_splits(synthetic_aoi: dict):
    """Sweep на любом сплите, кроме val, запрещён."""
    with pytest.raises(ValueError):
        run_sweep(
            synthetic_aoi["cv"], synthetic_aoi["pipeline"], split="all", predictor=MockPredictor()
        )
