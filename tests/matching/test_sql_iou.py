"""SQL-функция geoqc_iou на синтетических геометриях (метры → EPSG:4326 → обратно в UTM)."""

from __future__ import annotations

import pytest

from .conftest import METRIC_SRID, multi, sq, to_wgs_wkt

pytestmark = pytest.mark.db


def _iou(db, a, b) -> float:
    row = db.one(
        "SELECT geoqc_iou(ST_GeomFromText(%s, 4326), ST_GeomFromText(%s, 4326), %s) AS iou",
        (to_wgs_wkt(a), to_wgs_wkt(b), METRIC_SRID),
    )
    return float(row["iou"])


def test_iou_identical_is_one(db):
    """Одинаковые квадраты → IoU = 1."""
    assert _iou(db, sq(0, 0, 10, 10), sq(0, 0, 10, 10)) == pytest.approx(1.0)


def test_iou_half_shift_is_one_third(db):
    """Квадрат 10×10, сдвинутый на 5 м: ∩ = 50, ∪ = 150 → IoU = 1/3."""
    assert _iou(db, sq(0, 0, 10, 10), sq(5, 0, 15, 10)) == pytest.approx(1 / 3, abs=1e-6)


def test_iou_disjoint_and_touching_are_zero(db):
    """Непересекающиеся и касающиеся по ребру полигоны → 0."""
    assert _iou(db, sq(0, 0, 10, 10), sq(20, 0, 30, 10)) == 0.0
    assert _iou(db, sq(0, 0, 10, 10), sq(10, 0, 20, 10)) == pytest.approx(0.0, abs=1e-9)


def test_iou_multipolygon_uses_all_parts(db):
    """Мультиполигон из частей 100 + 50 м² против одной части 100 м² → 100/150 = 2/3."""
    ref = multi((0, 0, 10, 10), (20, 0, 30, 5))
    assert _iou(db, ref, sq(0, 0, 10, 10)) == pytest.approx(2 / 3, abs=1e-6)
    assert _iou(db, ref, ref) == pytest.approx(1.0)


def test_iou_is_symmetric_and_bounded(db):
    """IoU симметрична и лежит в [0, 1]."""
    a, b = sq(0, 0, 10, 10), sq(3, 3, 20, 20)
    assert _iou(db, a, b) == pytest.approx(_iou(db, b, a))
    assert 0.0 <= _iou(db, a, b) <= 1.0
    # ∩ = 49, ∪ = 100 + 289 - 49 = 340
    assert _iou(db, a, b) == pytest.approx(49 / 340, abs=1e-6)


def test_iou_null_propagates(db):
    """STRICT: NULL на входе → NULL."""
    row = db.one(
        "SELECT geoqc_iou(NULL::geometry, ST_GeomFromText(%s, 4326), %s) AS iou",
        (to_wgs_wkt(sq(0, 0, 1, 1)), METRIC_SRID),
    )
    assert row["iou"] is None
