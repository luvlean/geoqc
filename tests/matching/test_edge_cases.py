"""Краевые случаи: граница AOI, дедупликация цепочек, загрузка AOI."""

from __future__ import annotations

import json

import pytest
from shapely.geometry import mapping

from geoqc.matching import edge_cases
from geoqc.matching.matcher import run_matching

from .conftest import METRIC_SRID, sq, to_wgs

pytestmark = pytest.mark.db
MV = "m-test"


def _aoi(x0, y0, x1, y1) -> str:
    return json.dumps(mapping(to_wgs(sq(x0, y0, x1, y1))))


def test_aoi_edge_flag_lowers_confidence(db, db_conn, cfg):
    """Det, пересекающий границу AOI (буфер 10 м) → aoi_edge и confidence × 0.5 против такого же det внутри."""
    aoi = _aoi(0, 0, 200, 200)
    d_in = db.det(sq(50, 50, 60, 60), score=0.8)
    d_edge = db.det(sq(195, 50, 205, 60), score=0.8)  # crosses x = 200
    d_zone = db.det(sq(185, 100, 195, 110), score=0.8)  # inside AOI but within 10 m buffer
    res = run_matching(db_conn, MV, cfg=cfg, aoi_geojson=aoi)
    assert res.aoi_used is True
    assert res.n_aoi_edge == 2
    rows = {x["det_id"]: x for x in db.discrepancies()}
    assert rows[d_edge]["confidence"] == pytest.approx(0.5 * rows[d_in]["confidence"])
    assert rows[d_zone]["confidence"] == pytest.approx(0.5 * rows[d_in]["confidence"])
    flags = {
        r["det_id"]: r["aoi_edge"]
        for r in db.rows(
            "SELECT x.det_id, f.aoi_edge FROM discrepancy_features f JOIN discrepancies x ON x.id = f.discrepancy_id"
        )
    }
    assert flags == {d_in: False, d_edge: True, d_zone: True}


def test_refs_outside_aoi_are_not_missing_on_image(db, db_conn, cfg):
    """Эталоны OSM вне AOI (нет снимка) не классифицируются; ref на границе → missing_on_image с aoi_edge."""
    aoi = _aoi(0, 0, 100, 100)
    r_in = db.ref(sq(40, 40, 50, 50), osm_id=1)
    r_out = db.ref(sq(300, 300, 310, 310), osm_id=2)
    r_edge = db.ref(sq(95, 10, 105, 20), osm_id=3)
    db.det(sq(50, 50, 60, 60))  # unrelated det so the model version exists
    run_matching(db_conn, MV, cfg=cfg, aoi_geojson=aoi)
    rows = {x["ref_id"]: x for x in db.discrepancies() if x["class"] == "missing_on_image"}
    assert set(rows) == {r_in, r_edge}
    assert r_out not in rows
    assert rows[r_edge]["confidence"] == pytest.approx(0.5 * rows[r_in]["confidence"])


def test_without_aoi_everything_is_classified(db, db_conn, cfg):
    """Без AOI (файла нет) флаг не ставится и все эталоны участвуют."""
    db.ref(sq(300, 300, 310, 310), osm_id=2)
    db.det(sq(50, 50, 60, 60))
    res = run_matching(db_conn, MV, cfg=cfg)
    assert res.aoi_used is False and res.n_aoi_edge == 0
    assert db.by_class()["missing_on_image"] == 1


def test_dedup_chain_is_proper_nms(db, db_conn, cfg):
    """NMS по убыванию score: A(0.9)~B(0.8) и B~C(0.7), но A!~C → подавлен только B, C остаётся."""
    a = db.det(sq(0, 0, 10, 10), score=0.9)
    b = db.det(sq(1, 0, 11, 10), score=0.8)  # IoU(A,B) = 9/11 = 0.818
    c = db.det(sq(2, 0, 12, 10), score=0.7)  # IoU(B,C) = 9/11 = 0.818, IoU(A,C) = 8/12 = 0.667
    n = edge_cases.dedup_detections(db_conn, MV, METRIC_SRID, 0.7)
    assert n == 1
    sup = db.rows("SELECT det_id, kept_det_id FROM det_suppressed ORDER BY det_id")
    assert sup == [{"det_id": b, "kept_det_id": a}]
    assert c not in {s["det_id"] for s in sup}
    # rerun is idempotent
    assert edge_cases.dedup_detections(db_conn, MV, METRIC_SRID, 0.7) == 1
    assert db.one("SELECT count(*) AS n FROM det_suppressed")["n"] == 1


def test_small_and_nested_fragments_are_suppressed(db, db_conn):
    """Осколок < 12 м² и кусок крыши (≤ 25 % площади) подавляются; крупное вложенное и совпавшее с эталоном — нет."""
    house = db.det(sq(0, 0, 20, 20), score=0.9)  # 400 m2
    roof_piece = db.det(sq(2, 2, 8, 8), score=0.5)  # 36 m2 = 9 % of the house, fully inside
    sliver = db.det(sq(100, 0, 103, 3), score=0.6)  # 9 m2 standalone
    block = db.det(sq(200, 0, 240, 20), score=0.9)  # 800 m2, a merged block
    # 320 m2 = 40 % of the block: may be a building
    inner_house = db.det(sq(202, 2, 222, 18), score=0.8)
    # 36 m2 inside the block, but it matches a mapped building: IoU = 36 / 39 = 0.92
    shed = db.det(sq(230, 5, 236, 11), score=0.7)
    db.ref(sq(230, 5, 236, 11.5), osm_id=7)
    n = edge_cases.suppress_small_detections(db_conn, MV, METRIC_SRID, 12.0, 0.8, 0.25, 0.5)
    assert n == 2
    sup = {
        r["det_id"]: r for r in db.rows("SELECT det_id, kept_det_id, reason FROM det_suppressed")
    }
    assert set(sup) == {roof_piece, sliver}
    assert (sup[roof_piece]["reason"], sup[roof_piece]["kept_det_id"]) == ("nested", house)
    assert (sup[sliver]["reason"], sup[sliver]["kept_det_id"]) == ("small", None)
    assert {house, block, inner_house, shed}.isdisjoint(sup)


def test_suppressed_fragments_do_not_reach_the_queue(db, db_conn, cfg):
    """С suppress_small_detections осколки не становятся missing_in_map; без флага — становятся."""
    db.det(sq(0, 0, 20, 20), score=0.9)
    db.det(sq(2, 2, 8, 8), score=0.5)
    db.det(sq(100, 0, 103, 3), score=0.6)
    run_matching(db_conn, MV, cfg=cfg)
    assert db.by_class()["missing_in_map"] == 3
    cfg["matching"].update(
        suppress_small_detections=True,
        min_det_area_m2=12,
        nested_cover=0.8,
        nested_max_area_ratio=0.25,
    )
    res = run_matching(db_conn, MV, cfg=cfg)
    assert db.by_class()["missing_in_map"] == 1
    assert res.n_det_suppressed == 2


def test_split_multipart_is_idempotent(db, db_conn):
    """Разбиение мультиполигонов: второй вызов ничего не меняет."""
    from .conftest import multi

    db.det(multi((0, 0, 10, 10), (20, 0, 30, 10), (40, 0, 50, 10)))
    db.det(sq(100, 0, 110, 10))
    assert edge_cases.split_multipart_detections(db_conn, MV) == 1
    assert db.one("SELECT count(*) AS n FROM buildings_det")["n"] == 4
    assert edge_cases.split_multipart_detections(db_conn, MV) == 0
    assert db.one("SELECT max(ST_NumGeometries(geom)) AS m FROM buildings_det")["m"] == 1


def test_load_aoi_geojson_variants(tmp_path):
    """load_aoi_geojson принимает геометрию, Feature и FeatureCollection; отсутствие файла → None."""
    geom = mapping(to_wgs(sq(0, 0, 10, 10)))
    p1 = tmp_path / "g.geojson"
    p1.write_text(json.dumps(geom))
    p2 = tmp_path / "f.geojson"
    p2.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": geom}))
    p3 = tmp_path / "fc.geojson"
    p3.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {}, "geometry": geom}],
            }
        )
    )
    for p in (p1, p2, p3):
        out = edge_cases.load_aoi_geojson(p)
        assert out is not None
        assert json.loads(out)["type"] in {"Polygon", "MultiPolygon"}
    assert edge_cases.load_aoi_geojson(tmp_path / "missing.geojson") is None
    assert edge_cases.load_aoi_geojson(None) is None
