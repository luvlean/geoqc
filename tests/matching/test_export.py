"""Экспорт очереди проверки в GeoJSON по контракту §3.8."""

from __future__ import annotations

import json

import pytest

from geoqc.matching.export import PROPERTIES, export_discrepancies, fetch_features
from geoqc.matching.matcher import run_matching

from .conftest import sq

pytestmark = pytest.mark.db
MV = "m-test"


def test_export_geojson_contract(db, db_conn, cfg, tmp_path):
    """Файл — FeatureCollection в EPSG:4326, properties ровно из контракта, сортировка по confidence DESC."""
    db.ref(sq(0, 0, 10, 10), osm_id=111)  # missing_on_image
    db.ref(sq(100, 0, 110, 10), osm_id=222)
    db.det(sq(105, 0, 115, 10), score=0.9)  # geometry_mismatch
    db.det(sq(200, 0, 210, 10), score=0.95)  # missing_in_map
    db.det(sq(300, 0, 340, 40), score=0.2)  # missing_in_map, low score
    run_matching(db_conn, MV, cfg=cfg)

    out = tmp_path / "discrepancies.geojson"
    n = export_discrepancies(db_conn, out, model_version=MV)
    assert n == 4
    fc = json.loads(out.read_text(encoding="utf-8"))
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 4
    confs = [f["properties"]["confidence"] for f in fc["features"]]
    assert confs == sorted(confs, reverse=True)
    for f in fc["features"]:
        assert f["type"] == "Feature"
        assert f["geometry"]["type"] == "MultiPolygon"
        assert set(f["properties"]) == set(PROPERTIES)
        assert f["properties"]["status"] == "new"
        assert f["properties"]["model_version"] == MV
        assert f["properties"]["created_at"]
        lon, lat = f["geometry"]["coordinates"][0][0][0]
        assert -180 <= lon <= 180 and -90 <= lat <= 90
    by_class = {
        f["properties"]["class"]: f["properties"]
        for f in fc["features"]
        if f["properties"]["class"] != "missing_in_map"
    }
    assert by_class["missing_on_image"]["ref_osm_id"] == 111
    assert by_class["geometry_mismatch"]["ref_osm_id"] == 222
    assert by_class["geometry_mismatch"]["iou"] == pytest.approx(1 / 3, abs=1e-6)


def test_export_filters(db, db_conn, cfg, tmp_path):
    """Фильтры class / min_conf / status / limit."""
    db.ref(sq(0, 0, 10, 10), osm_id=1)
    db.det(sq(200, 0, 210, 10), score=0.95)
    db.det(sq(300, 0, 310, 10), score=0.1)
    run_matching(db_conn, MV, cfg=cfg)
    assert len(fetch_features(db_conn, classes=["missing_on_image"])) == 1
    assert len(fetch_features(db_conn, classes=["missing_in_map"])) == 2
    assert len(fetch_features(db_conn, statuses=["confirmed"])) == 0
    assert len(fetch_features(db_conn, limit=1)) == 1
    hi = fetch_features(db_conn, classes=["missing_in_map"], min_conf=0.5)
    assert len(hi) == 1
    assert export_discrepancies(db_conn, tmp_path / "x.geojson", classes=["geometry_mismatch"]) == 0
