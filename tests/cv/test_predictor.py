"""Интерфейс Predictor: мок, выбор устройства, компоненты, U-Net на случайных весах."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from geoqc.cv.predictor import (
    Mask,
    MockPredictor,
    build_predictor,
    components_to_masks,
    resolve_device,
)


def test_resolve_device_cpu_and_auto():
    """resolve_device('cpu') -> cpu; 'auto' -> одно из cuda/mps/cpu; недоступные -> cpu."""
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("auto") in {"cuda", "mps", "cpu"}
    assert resolve_device(None) in {"cuda", "mps", "cpu"}


def test_components_to_masks_orders_by_area_and_scores_by_mean_prob():
    """Компоненты 8-связности сортируются по убыванию площади; score = средняя вероятность."""
    binary = np.zeros((20, 20), dtype=bool)
    binary[0:5, 0:5] = True  # 25 px
    binary[10:12, 10:12] = True  # 4 px
    prob = np.zeros((20, 20), dtype=float)
    prob[0:5, 0:5] = 0.7
    prob[10:12, 10:12] = 0.9
    masks = components_to_masks(binary, prob, min_component_px=1)
    assert [m.area_px for m in masks] == [25, 4]
    assert abs(masks[0].score - 0.7) < 1e-9 and abs(masks[1].score - 0.9) < 1e-9
    assert components_to_masks(binary, prob, min_component_px=5)[0].area_px == 25
    assert components_to_masks(np.zeros((4, 4), dtype=bool)) == []


def test_mock_predictor_detects_bright_blobs_deterministically():
    """MockPredictor находит яркие блобы, score растёт с яркостью, повторный вызов идентичен."""
    tile = np.full((64, 64, 3), 30, dtype=np.uint8)
    tile[5:15, 5:15] = 255
    tile[30:40, 30:40] = 220
    pred = MockPredictor(threshold=200, min_component_px=1)
    masks = pred.predict(tile)
    assert len(masks) == 2 and all(isinstance(m, Mask) for m in masks)
    scores = sorted(m.score for m in masks)
    assert scores[1] == pytest.approx(1.0) and scores[0] == pytest.approx(0.5 + 0.5 * 220 / 255)
    again = pred.predict(tile)
    assert all(
        np.array_equal(a.array, b.array) and a.score == b.score
        for a, b in zip(masks, again, strict=True)
    )
    assert len(pred.predict_batch([tile, tile])) == 2


def test_mock_predictor_rejects_wrong_shape():
    """Одноканальный массив -> ValueError (интерфейс требует H x W x 3)."""
    with pytest.raises(ValueError):
        MockPredictor().predict(np.zeros((8, 8), dtype=np.uint8))


def test_build_predictor_mock_and_unknown_backend():
    """Фабрика собирает mock по конфигу и падает на неизвестном backend."""
    p = build_predictor({"backend": "mock", "mock": {"threshold": 100}})
    assert isinstance(p, MockPredictor) and p.threshold == 100
    with pytest.raises(ValueError):
        build_predictor({"backend": "nope"})


def test_ultralytics_backend_requires_weights(tmp_path: Path):
    """Ultralytics-бэкенд без файла весов -> FileNotFoundError с подсказкой fetch-weights."""
    from geoqc.cv.predictor import UltralyticsSegPredictor

    with pytest.raises(FileNotFoundError, match="fetch-weights"):
        UltralyticsSegPredictor(tmp_path / "missing.pt", device="cpu")


@pytest.mark.slow
def test_ultralytics_predictor_runs_when_weights_present(synthetic_aoi: dict):
    """Реальный YOLOv8-seg бэкенд (если веса скачаны) отрабатывает на синтетическом тайле без ошибок и в batch-режиме."""
    from geoqc.config import REPO_ROOT
    from geoqc.cv.predictor import UltralyticsSegPredictor
    from geoqc.cv.tiles import read_tile

    weights = REPO_ROOT / "models" / "yolov8m-building-seg.pt"
    if not weights.exists():
        pytest.skip("weights not fetched (run `geoqc cv fetch-weights`)")
    pred = UltralyticsSegPredictor(weights, conf=0.05, device="cpu", imgsz=512)
    assert pred.class_names == {0: "Building"}
    tile = read_tile(synthetic_aoi["tiles_dir"] / "0_0.tif", "0_0")
    masks = pred.predict(tile.image)
    assert isinstance(masks, list)
    for m in masks:
        assert m.array.shape == tile.shape and m.array.dtype == bool and 0.0 <= m.score <= 1.0
    batch = pred.predict_batch([tile.image, tile.image])
    assert len(batch) == 2 and len(batch[0]) == len(batch[1])
