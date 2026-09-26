"""Склейка детекций на перекрытии тайлов: IoU/overlap-критерий, score, транзитивность."""

from __future__ import annotations

import pytest
from shapely.geometry import box

from geoqc.cv.vectorize import Detection, merge_overlaps, pair_overlap


def test_pair_overlap_formulas():
    """IoU двух квадратов 10х10 со сдвигом 5 = 1/3, overlap ratio = 0.5."""
    iou, ratio = pair_overlap(box(0, 0, 10, 10), box(5, 0, 15, 10))
    assert abs(iou - 1 / 3) < 1e-9
    assert abs(ratio - 0.5) < 1e-9
    assert pair_overlap(box(0, 0, 1, 1), box(5, 5, 6, 6)) == (0.0, 0.0)


def test_building_split_across_two_tiles_is_merged_once():
    """Здание 30х30 м, полностью в тайле A и частично (10 м) в тайле B -> одна детекция, площадь 900 м²."""
    full = Detection(box(210, 0, 240, 30), 0.6, "0_0")
    part = Detection(box(224, 0, 240, 30), 0.9, "0_1")  # visible slice inside tile B
    merged = merge_overlaps(
        [full, part], iou_threshold=0.1, min_overlap_ratio=0.5, score_mode="max"
    )
    assert len(merged) == 1
    assert abs(merged[0].area - 900.0) < 1e-6
    assert merged[0].score == 0.9  # max
    assert merged[0].tile_id == "0_0"  # largest member


def test_area_weighted_score():
    """score_mode=area_weighted: (0.6*900 + 0.9*480) / (900+480)."""
    a = Detection(box(210, 0, 240, 30), 0.6, "0_0")
    b = Detection(box(224, 0, 240, 30), 0.9, "0_1")
    merged = merge_overlaps([a, b], score_mode="area_weighted")
    expected = (0.6 * 900 + 0.9 * 480) / (900 + 480)
    assert abs(merged[0].score - expected) < 1e-9


def test_low_overlap_below_both_thresholds_is_not_merged():
    """Два соседних здания, касающихся углами (IoU 0.02, ratio 0.1) — остаются раздельными."""
    a = Detection(box(0, 0, 10, 10), 0.5, "0_0")
    b = Detection(box(9, 9, 19, 19), 0.5, "0_1")  # 1 m² overlap
    merged = merge_overlaps([a, b], iou_threshold=0.1, min_overlap_ratio=0.5)
    assert len(merged) == 2


def test_same_tile_pairs_are_not_merged_by_default_but_can_be():
    """Пары внутри одного тайла не склеиваются (NMS уже отработал), если не включить merge_same_tile."""
    a = Detection(box(0, 0, 10, 10), 0.5, "0_0")
    b = Detection(box(2, 2, 12, 12), 0.7, "0_0")
    assert len(merge_overlaps([a, b])) == 2
    assert len(merge_overlaps([a, b], merge_same_tile=True)) == 1


def test_transitive_merge_over_three_tiles():
    """Здание, разрезанное тремя тайлами по цепочке, собирается в один полигон (union-find)."""
    a = Detection(box(0, 0, 12, 10), 0.5, "0_0")
    b = Detection(box(8, 0, 22, 10), 0.6, "0_1")
    c = Detection(box(18, 0, 30, 10), 0.7, "0_2")
    merged = merge_overlaps([a, b, c], iou_threshold=0.1, min_overlap_ratio=0.3)
    assert len(merged) == 1
    assert abs(merged[0].area - 300.0) < 1e-6
    assert merged[0].score == 0.7


def test_invalid_score_mode():
    """Неизвестный score_mode -> ValueError."""
    with pytest.raises(ValueError):
        merge_overlaps(
            [Detection(box(0, 0, 1, 1), 0.5, "a"), Detection(box(0, 0, 1, 1), 0.5, "b")],
            score_mode="median",
        )
