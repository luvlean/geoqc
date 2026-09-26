"""Чистые тесты алгоритмов назначения 1:1 (жадный vs венгерский) на числовых матрицах IoU."""

from __future__ import annotations

from geoqc.matching.matcher import greedy_assign, hungarian_assign


def test_greedy_takes_best_first_and_respects_one_to_one():
    """Жадный: пары по убыванию IoU, каждая сторона используется один раз."""
    pairs = [(1, 10, 0.9), (1, 11, 0.8), (2, 11, 0.7), (2, 10, 0.6)]
    assert greedy_assign(pairs) == [(1, 10, 0.9), (2, 11, 0.7)]


def test_greedy_tie_break_is_deterministic():
    """При равном IoU порядок — по ref_id, затем det_id (как в SQL ORDER BY)."""
    pairs = [(2, 10, 0.5), (1, 10, 0.5), (1, 11, 0.5)]
    assert greedy_assign(pairs) == [(1, 10, 0.5), (2, 10, 0.5)][:1] + []  # det 10 taken by ref 1
    assert greedy_assign(pairs)[0] == (1, 10, 0.5)


def test_hungarian_beats_greedy_on_conflict():
    """Конфликт: R1–D1 0.556 (лучшая), R1–D2 0.5, R2–D1 0.4. Жадный → 1 пара; венгерский → 2 пары."""
    pairs = [(1, 1, 0.556), (1, 2, 0.5), (2, 1, 0.4)]
    g = greedy_assign(pairs)
    h = hungarian_assign(pairs)
    assert g == [(1, 1, 0.556)]
    assert sorted(h) == [(1, 2, 0.5), (2, 1, 0.4)]
    assert sum(p[2] for p in h) > sum(p[2] for p in g)


def test_hungarian_min_iou_filter_and_clusters():
    """Независимые кластеры решаются отдельно; пары ниже min_iou отбрасываются."""
    pairs = [(1, 1, 0.9), (2, 2, 0.3), (3, 3, 0.6), (3, 4, 0.55), (4, 3, 0.55)]
    h = hungarian_assign(pairs, min_iou=0.5)
    assert (2, 2, 0.3) not in h
    assert (1, 1, 0.9) in h
    # cluster {3,4}x{3,4}: optimum is (3,4)+(4,3) = 1.1 over (3,3) = 0.6
    assert (3, 4, 0.55) in h and (4, 3, 0.55) in h
    assert len(h) == 3


def test_hungarian_empty():
    """Пустой вход → пустой результат."""
    assert hungarian_assign([]) == []
    assert greedy_assign([]) == []
