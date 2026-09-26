"""Чистые тесты формул scoring.py на конкретных числах (без БД)."""

from __future__ import annotations

import math

import pytest

from geoqc.matching.scoring import (
    Features,
    area_norm,
    confidence_geometry_mismatch,
    confidence_missing_in_map,
    confidence_missing_on_image,
    normalize_weights,
    score_features,
)

from .conftest import MATCHING_CFG

LN10_LN250 = math.log(10) / math.log(250)  # area_norm(200 m²) for [20, 5000]


def test_area_norm_values():
    """area_norm = clip(log(area/min)/log(max/min), 0, 1): 200 m² -> ln10/ln250 ≈ 0.417."""
    assert area_norm(200, 20, 5000) == pytest.approx(LN10_LN250)
    assert area_norm(20, 20, 5000) == 0.0
    assert area_norm(5000, 20, 5000) == 1.0
    assert area_norm(5, 20, 5000) == 0.0  # below min -> clipped
    assert area_norm(1e6, 20, 5000) == 1.0  # above max -> clipped
    assert area_norm(0, 20, 5000) == 0.0
    assert area_norm(math.sqrt(20 * 5000), 20, 5000) == pytest.approx(0.5)


def test_area_norm_rejects_bad_bounds():
    """Некорректные границы площади — ошибка конфигурации."""
    with pytest.raises(ValueError):
        area_norm(100, 0, 5000)
    with pytest.raises(ValueError):
        area_norm(100, 5000, 20)


def test_normalize_weights():
    """Веса нормируются к сумме 1; при отсутствии второго источника член secondary выбрасывается."""
    w = normalize_weights({"score": 0.5, "area": 0.2, "secondary": 0.3})
    assert w == pytest.approx({"score": 0.5, "area": 0.2, "secondary": 0.3})
    w2 = normalize_weights({"score": 0.5, "area": 0.2, "secondary": 0.3}, drop=("secondary",))
    assert w2 == pytest.approx({"score": 5 / 7, "area": 2 / 7})
    with pytest.raises(ValueError):
        normalize_weights({"a": 0.0})


def test_missing_in_map_formula():
    """missing_in_map: 0.5·0.8 + 0.2·0.41702 + 0.3·1.0 = 0.783404."""
    w = {"score": 0.5, "area": 0.2, "secondary": 0.3}
    conf = confidence_missing_in_map(0.8, LN10_LN250, 1.0, w)
    assert conf == pytest.approx(0.4 + 0.2 * LN10_LN250 + 0.3)
    assert conf == pytest.approx(0.783404, abs=1e-6)
    # without secondary agreement the last term vanishes
    assert confidence_missing_in_map(0.8, LN10_LN250, 0.0, w) == pytest.approx(
        0.4 + 0.2 * LN10_LN250
    )


def test_missing_on_image_formula():
    """missing_on_image: 0.3·a + 0.4·(1 − overlap) + 0.3·(1 − max_iou); (1.0, 0.25, 0.05) → 0.885."""
    w = {"area": 0.3, "secondary": 0.4, "context": 0.3}
    assert confidence_missing_on_image(1.0, 0.25, 0.05, w) == pytest.approx(0.3 + 0.3 + 0.285)
    assert confidence_missing_on_image(0.0, 1.0, 1.0, w) == 0.0
    assert confidence_missing_on_image(1.0, 0.0, 0.0, w) == pytest.approx(1.0)


def test_geometry_mismatch_formula():
    """geometry_mismatch: 0.4·score + 0.4·(1 − iou) + 0.2·a; (0.9, 1/3, 0.5) → 0.36 + 0.26667 + 0.1."""
    w = {"score": 0.4, "iou": 0.4, "area": 0.2}
    assert confidence_geometry_mismatch(0.9, 1 / 3, 0.5, w) == pytest.approx(
        0.36 + 0.4 * (2 / 3) + 0.1
    )
    assert confidence_geometry_mismatch(1.0, 0.0, 1.0, w) == pytest.approx(1.0)
    assert confidence_geometry_mismatch(0.0, 1.0, 0.0, w) == 0.0


def test_score_features_end_to_end_with_config():
    """score_features воспроизводит формулы через конфиг; результат всегда в [0, 1]."""
    f = Features(
        cls="missing_in_map", score=0.8, area_m2=200, secondary_coverage=1.0, has_secondary=True
    )
    s = score_features(f, MATCHING_CFG)
    assert s.area_norm == pytest.approx(LN10_LN250)
    assert s.confidence == pytest.approx(0.783404, abs=1e-6)
    assert s.confidence_raw == s.confidence

    g = Features(cls="geometry_mismatch", score=0.9, area_m2=math.sqrt(20 * 5000), iou=1 / 3)
    assert score_features(g, MATCHING_CFG).confidence == pytest.approx(0.36 + 0.4 * (2 / 3) + 0.1)

    m = Features(
        cls="missing_on_image", area_m2=5000, iou=0.05, secondary_coverage=0.25, has_secondary=True
    )
    assert score_features(m, MATCHING_CFG).confidence == pytest.approx(0.885)


def test_score_features_without_secondary_renormalises():
    """Без второго источника: missing_in_map = (0.5·score + 0.2·a) / 0.7."""
    f = Features(
        cls="missing_in_map", score=0.8, area_m2=200, secondary_coverage=0.0, has_secondary=False
    )
    s = score_features(f, MATCHING_CFG)
    assert s.confidence == pytest.approx((0.5 * 0.8 + 0.2 * LN10_LN250) / 0.7)
    m = Features(cls="missing_on_image", area_m2=5000, iou=0.0, has_secondary=False)
    assert score_features(m, MATCHING_CFG).confidence == pytest.approx(1.0)


def test_aoi_edge_factor_halves_confidence():
    """Флаг границы AOI умножает confidence на aoi_edge_factor (0.5), confidence_raw не меняется."""
    f = Features(
        cls="missing_in_map", score=0.8, area_m2=200, secondary_coverage=1.0, has_secondary=True
    )
    e = Features(
        cls="missing_in_map",
        score=0.8,
        area_m2=200,
        secondary_coverage=1.0,
        has_secondary=True,
        aoi_edge=True,
    )
    s, se = score_features(f, MATCHING_CFG), score_features(e, MATCHING_CFG)
    assert se.confidence_raw == pytest.approx(s.confidence)
    assert se.confidence == pytest.approx(0.5 * s.confidence)


def test_unknown_class_rejected():
    """Неизвестный класс расхождения — ошибка."""
    with pytest.raises(ValueError):
        score_features(Features(cls="other", area_m2=10), MATCHING_CFG)
