"""Сценарии сопоставления и классификации на синтетических геометриях (фикстура db_conn)."""

from __future__ import annotations

import pytest

from geoqc.matching.matcher import run_matching

from .conftest import METRIC_SRID, multi, sq

pytestmark = pytest.mark.db
MV = "m-test"


def test_exact_match_goes_to_matches_only(db, db_conn, cfg):
    """Полное совпадение ref и det → одна запись в matches с IoU = 1, discrepancies пуст."""
    r = db.ref(sq(0, 0, 10, 10), osm_id=1)
    d = db.det(sq(0, 0, 10, 10))
    res = run_matching(db_conn, MV, cfg=cfg)
    assert res.n_matches == 1
    assert db.matches() == [{"ref_id": r, "det_id": d, "iou": pytest.approx(1.0)}]
    assert db.discrepancies() == []
    assert res.counts_by_class == {
        "missing_in_map": 0,
        "missing_on_image": 0,
        "geometry_mismatch": 0,
    }
    assert res.mean_iou_matched == pytest.approx(1.0)


def test_half_shift_is_geometry_mismatch(db, db_conn, cfg):
    """Сдвиг на половину (IoU = 1/3) → geometry_mismatch с ref_id, det_id и iou пары; matches пуст."""
    r = db.ref(sq(0, 0, 10, 10), osm_id=1)
    d = db.det(sq(5, 0, 15, 10), score=0.9)
    run_matching(db_conn, MV, cfg=cfg)
    assert db.matches() == []
    rows = db.discrepancies()
    assert len(rows) == 1
    x = rows[0]
    assert (x["class"], x["ref_id"], x["det_id"]) == ("geometry_mismatch", r, d)
    assert x["iou"] == pytest.approx(1 / 3, abs=1e-6)
    assert 0.0 <= x["confidence"] <= 1.0
    # geometry of the discrepancy covers both footprints (union)
    g = db.one(
        "SELECT ST_Area(ST_Transform(geom, %s)) AS a, ST_GeometryType(geom) AS t FROM discrepancies",
        (METRIC_SRID,),
    )
    assert g["t"] == "ST_MultiPolygon"
    assert g["a"] == pytest.approx(150.0, rel=1e-6)


def test_det_without_ref_is_missing_in_map(db, db_conn, cfg):
    """Детекция без эталона → missing_in_map (ref_id NULL, iou NULL, геометрия детекции)."""
    d = db.det(sq(0, 0, 10, 10), score=0.75)
    run_matching(db_conn, MV, cfg=cfg)
    rows = db.discrepancies()
    assert [(x["class"], x["ref_id"], x["det_id"], x["iou"]) for x in rows] == [
        ("missing_in_map", None, d, None)
    ]


def test_ref_without_det_is_missing_on_image(db, db_conn, cfg):
    """Эталон OSM без детекции → missing_on_image (det_id NULL, геометрия эталона)."""
    r = db.ref(sq(0, 0, 10, 10), osm_id=42)
    # a detection far away so that the model version exists
    db.det(sq(500, 500, 510, 510))
    run_matching(db_conn, MV, cfg=cfg)
    by = {x["class"]: x for x in db.discrepancies()}
    assert set(by) == {"missing_on_image", "missing_in_map"}
    assert by["missing_on_image"]["ref_id"] == r
    assert by["missing_on_image"]["det_id"] is None
    assert by["missing_on_image"]["iou"] is None


def test_low_iou_pair_below_mismatch_min(db, db_conn, cfg):
    """Пересечение с IoU < 0.1 не образует пары: det → missing_in_map, ref → missing_on_image, iou = max IoU."""
    db.ref(sq(0, 0, 10, 10), osm_id=1)
    db.det(sq(9, 9, 19, 19))  # ∩ = 1, ∪ = 199 → IoU ≈ 0.005
    run_matching(db_conn, MV, cfg=cfg)
    by = {x["class"]: x for x in db.discrepancies()}
    assert set(by) == {"missing_on_image", "missing_in_map"}
    assert by["missing_in_map"]["iou"] == pytest.approx(1 / 199, abs=1e-5)
    assert by["missing_on_image"]["iou"] == pytest.approx(1 / 199, abs=1e-5)


def test_two_dets_on_one_ref_without_dedup(db, db_conn, cfg):
    """Два det на один ref (IoU между ними ниже dedup_iou): лучший → match, второй → missing_in_map."""
    cfg["matching"]["dedup_detections"] = False
    r = db.ref(sq(0, 0, 10, 10), osm_id=1)
    d1 = db.det(sq(0, 0, 10, 10), score=0.9)  # IoU 1 with ref
    d2 = db.det(sq(6, 0, 16, 10), score=0.8)  # IoU with ref 4/16 = 0.25, with d1 0.25
    run_matching(db_conn, MV, cfg=cfg)
    assert db.matches() == [{"ref_id": r, "det_id": d1, "iou": pytest.approx(1.0)}]
    rows = db.discrepancies()
    assert len(rows) == 1
    assert (rows[0]["class"], rows[0]["det_id"]) == ("missing_in_map", d2)
    assert rows[0]["iou"] == pytest.approx(0.25, abs=1e-6)  # max IoU with any ref, for the record


def test_two_near_duplicate_dets_are_deduplicated(db, db_conn, cfg):
    """Два почти одинаковых det (IoU 0.9 > dedup_iou): слабый подавлен и не попадает в discrepancies."""
    r = db.ref(sq(0, 0, 10, 10), osm_id=1)
    d1 = db.det(sq(0, 0, 10, 10), score=0.9)
    d2 = db.det(sq(0, 0, 10, 9), score=0.6)  # IoU with d1 = 0.9
    res = run_matching(db_conn, MV, cfg=cfg)
    assert res.n_det_suppressed == 1
    sup = db.rows("SELECT det_id, kept_det_id FROM det_suppressed")
    assert sup == [{"det_id": d2, "kept_det_id": d1}]
    assert db.matches() == [{"ref_id": r, "det_id": d1, "iou": pytest.approx(1.0)}]
    assert db.discrepancies() == []
    # buildings_det rows themselves are untouched
    assert db.one("SELECT count(*) AS n FROM buildings_det")["n"] == 2


def test_multipart_detection_is_split_and_matched_per_part(db, db_conn, cfg):
    """Мультиполигон-детекция из двух частей → по частям: две пары match с двумя эталонами."""
    r1 = db.ref(sq(0, 0, 10, 10), osm_id=1)
    r2 = db.ref(sq(20, 0, 30, 10), osm_id=2)
    db.det(multi((0, 0, 10, 10), (20, 0, 30, 10)), score=0.9)
    res = run_matching(db_conn, MV, cfg=cfg)
    assert res.n_det_split == 1
    assert db.one("SELECT count(*) AS n FROM buildings_det")["n"] == 2
    m = db.matches()
    assert {x["ref_id"] for x in m} == {r1, r2}
    assert all(x["iou"] == pytest.approx(1.0) for x in m)
    assert db.discrepancies() == []


def test_multipart_reference_is_kept_whole(db, db_conn, cfg):
    """Мультиполигон-эталон не режется: одна часть детектирована → IoU = 100/150 → match."""
    r = db.ref(multi((0, 0, 10, 10), (20, 0, 30, 5)), osm_id=7)
    d = db.det(sq(0, 0, 10, 10))
    run_matching(db_conn, MV, cfg=cfg)
    assert db.matches() == [{"ref_id": r, "det_id": d, "iou": pytest.approx(2 / 3, abs=1e-6)}]


def test_secondary_source_is_not_a_reference(db, db_conn, cfg):
    """Microsoft/Google footprints не участвуют как эталон: одиночный microsoft-ref не даёт missing_on_image."""
    db.ref(sq(0, 0, 10, 10), source="microsoft")
    db.ref(sq(100, 0, 110, 10), source="google")
    d = db.det(sq(200, 0, 210, 10))
    run_matching(db_conn, MV, cfg=cfg)
    rows = db.discrepancies()
    assert [(x["class"], x["det_id"]) for x in rows] == [("missing_in_map", d)]
    assert db.matches() == []


def test_secondary_agreement_raises_missing_in_map_confidence(db, db_conn, cfg):
    """Согласие со вторым источником повышает confidence missing_in_map при прочих равных."""
    db.ref(sq(0, 0, 10, 10), source="microsoft")  # covers d1 fully
    d1 = db.det(sq(0, 0, 10, 10), score=0.8)
    d2 = db.det(sq(100, 0, 110, 10), score=0.8)  # no secondary footprint
    res = run_matching(db_conn, MV, cfg=cfg)
    assert res.has_secondary is True
    conf = {x["det_id"]: x["confidence"] for x in db.discrepancies()}
    assert conf[d1] > conf[d2]
    assert conf[d1] - conf[d2] == pytest.approx(0.3, abs=1e-6)  # w_sec · (1 − 0)
    feats = db.rows(
        "SELECT f.secondary_coverage, f.has_secondary FROM discrepancy_features f JOIN discrepancies x ON x.id = f.discrepancy_id WHERE x.det_id = %s",
        (d1,),
    )
    assert feats[0]["secondary_coverage"] == pytest.approx(1.0, abs=1e-6)
    assert feats[0]["has_secondary"] is True


def test_other_model_version_is_ignored(db, db_conn, cfg):
    """Детекции другой model_version не участвуют и не удаляются при прогоне."""
    r = db.ref(sq(0, 0, 10, 10), osm_id=1)
    db.det(sq(0, 0, 10, 10), model_version="other")
    d = db.det(sq(50, 0, 60, 10), model_version=MV)
    run_matching(db_conn, MV, cfg=cfg)
    assert db.matches(MV) == []
    by = {x["class"]: x for x in db.discrepancies(MV)}
    assert by["missing_on_image"]["ref_id"] == r
    assert by["missing_in_map"]["det_id"] == d
    assert db.one("SELECT count(*) AS n FROM buildings_det WHERE model_version = 'other'")["n"] == 1
    assert db.one("SELECT count(*) AS n FROM discrepancies WHERE model_version = 'other'")["n"] == 0


def test_rerun_is_idempotent(db, db_conn, cfg):
    """Повторный run для той же model_version не дублирует записи: те же счётчики и пары."""
    db.ref(sq(0, 0, 10, 10), osm_id=1)
    db.ref(sq(100, 0, 110, 10), osm_id=2)
    db.ref(sq(300, 0, 310, 10), osm_id=3)
    db.det(sq(0, 0, 10, 10))
    db.det(sq(105, 0, 115, 10))
    db.det(sq(200, 0, 210, 10))
    db.det(sq(200, 0, 210, 9), score=0.3)  # duplicate
    db.det(multi((400, 0, 410, 10), (420, 0, 430, 10)))
    first = run_matching(db_conn, MV, cfg=cfg)
    snap1 = (db.matches(), [(x["class"], x["ref_id"], x["det_id"]) for x in db.discrepancies()])
    second = run_matching(db_conn, MV, cfg=cfg)
    snap2 = (db.matches(), [(x["class"], x["ref_id"], x["det_id"]) for x in db.discrepancies()])
    assert (
        first.counts_by_class
        == second.counts_by_class
        == {
            "geometry_mismatch": 1,
            "missing_in_map": 3,  # det 200 + two split parts
            "missing_on_image": 1,
        }
    )
    assert first.n_matches == second.n_matches == 1
    assert snap1 == snap2
    assert second.n_det_split == 0  # nothing left to split
    assert second.n_det_suppressed == 1
    assert db.one("SELECT count(*) AS n FROM discrepancies")["n"] == 5
    assert db.one("SELECT count(*) AS n FROM discrepancy_features")["n"] == 5


def test_greedy_vs_hungarian_on_conflict(db, db_conn, cfg):
    """Конфликт (порог 0.35): R1–D1 0.556, R1–D2 0.5, R2–D1 0.4. Жадный → 1 match, венгерский → 2."""
    cfg["matching"]["iou_match"] = 0.35
    cfg["matching"]["iou_mismatch_min"] = 0.1
    r1 = db.ref(sq(0, 0, 10, 10), osm_id=1)
    r2 = db.ref(sq(10, 0, 20, 10), osm_id=2)
    d1 = db.det(sq(0, 0, 18, 10), score=0.9)  # R1: 100/180 = 0.556, R2: 80/200 = 0.4
    d2 = db.det(sq(0, 0, 10, 5), score=0.9)  # R1: 50/100 = 0.5

    g = run_matching(db_conn, MV, cfg=cfg, algorithm="greedy")
    assert g.n_matches == 1
    assert db.matches() == [{"ref_id": r1, "det_id": d1, "iou": pytest.approx(100 / 180, abs=1e-6)}]
    # the loser d2 has no unmatched ref left -> missing_in_map; r2 -> missing_on_image
    assert db.by_class() == {"missing_in_map": 1, "missing_on_image": 1, "geometry_mismatch": 0}

    h = run_matching(db_conn, MV, cfg=cfg, algorithm="hungarian")
    assert h.n_matches == 2
    m = {x["ref_id"]: x for x in db.matches()}
    assert m[r1]["det_id"] == d2 and m[r1]["iou"] == pytest.approx(0.5, abs=1e-6)
    assert m[r2]["det_id"] == d1 and m[r2]["iou"] == pytest.approx(0.4, abs=1e-6)
    assert db.discrepancies() == []


def test_run_fails_loudly_for_unknown_model_version(db, db_conn, cfg):
    """Неизвестная model_version — ошибка, а не «все эталоны стали missing_on_image»."""
    db.ref(sq(0, 0, 10, 10), osm_id=1)
    db.det(sq(0, 0, 10, 10), model_version="m-a")
    with pytest.raises(RuntimeError, match="model_version"):
        run_matching(db_conn, "m-missing", cfg=cfg)
    assert db.one("SELECT count(*) AS n FROM discrepancies")["n"] == 0


def test_model_version_autodetected_when_single(db, db_conn, cfg):
    """Без --model-version берётся единственная версия из buildings_det."""
    db.ref(sq(0, 0, 10, 10), osm_id=1)
    db.det(sq(0, 0, 10, 10), model_version="m-only")
    res = run_matching(db_conn, None, cfg=cfg)
    assert res.model_version == "m-only"
    assert res.n_matches == 1
