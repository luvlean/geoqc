"""Очередь проверки на синтетических данных: фильтры, сортировка, карточка, статус, экспорт, статистика."""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.api.conftest import AOI_BBOX_STR

pytestmark = pytest.mark.db

EXPORT_KEYS = {
    "id",
    "class",
    "confidence",
    "iou",
    "status",
    "ref_osm_id",
    "model_version",
    "created_at",
}


def ids(fc: dict) -> list[int]:
    return [f["id"] for f in fc["features"]]


def test_list_bbox_sorted_by_confidence(client, seed):
    """Список по bbox AOI возвращает только объекты внутри рамки, по убыванию confidence."""
    resp = client.get(f"/discrepancies?bbox={AOI_BBOX_STR}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/geo+json")
    fc = resp.json()
    assert fc["type"] == "FeatureCollection"
    assert ids(fc) == [seed.d1, seed.d2, seed.d3]
    assert [f["properties"]["confidence"] for f in fc["features"]] == [0.9, 0.6, 0.3]
    assert fc["numberReturned"] == 3 and fc["numberMatched"] == 3
    first = fc["features"][0]
    assert first["type"] == "Feature" and first["geometry"]["type"] == "MultiPolygon"
    assert {
        "id",
        "class",
        "confidence",
        "iou",
        "status",
        "ref_osm_id",
        "det_id",
        "ref_id",
        "model_version",
        "created_at",
        "updated_at",
    } <= set(first["properties"])


def test_list_ref_osm_id_comes_from_buildings_ref(client, seed):
    """ref_osm_id берётся из buildings_ref через ref_id; у детекций без пары он null."""
    fc = client.get(f"/discrepancies?bbox={AOI_BBOX_STR}").json()
    by_id = {f["id"]: f["properties"] for f in fc["features"]}
    assert by_id[seed.d2]["ref_osm_id"] == 111
    assert by_id[seed.d3]["ref_osm_id"] == 222
    assert by_id[seed.d1]["ref_osm_id"] is None and by_id[seed.d1]["det_id"] == seed.det_a


def test_list_filter_class(client, seed):
    """Фильтр class принимает список через запятую."""
    assert ids(client.get("/discrepancies?class=missing_in_map").json()) == [seed.d1, seed.d4]
    assert ids(client.get("/discrepancies?class=missing_on_image,geometry_mismatch").json()) == [
        seed.d2,
        seed.d3,
    ]


def test_list_filter_min_conf(client, seed):
    """min_conf отсекает объекты с confidence ниже порога (граница включается)."""
    assert ids(client.get("/discrepancies?min_conf=0.7").json()) == [seed.d1, seed.d4]
    assert ids(client.get("/discrepancies?min_conf=0.75").json()) == [seed.d1, seed.d4]
    assert ids(client.get("/discrepancies?min_conf=0.91").json()) == []


def test_list_filter_status(client, seed):
    """Фильтр status: одно значение и список."""
    assert ids(client.get("/discrepancies?status=confirmed").json()) == [seed.d3]
    assert ids(client.get("/discrepancies?status=new,confirmed").json()) == [
        seed.d1,
        seed.d2,
        seed.d3,
    ]


def test_list_pagination(client, seed):
    """limit/offset листают отсортированный список; numberMatched считает все подходящие строки."""
    page = client.get("/discrepancies?limit=1&offset=1").json()
    assert ids(page) == [seed.d4]
    assert page["numberReturned"] == 1 and page["numberMatched"] == 4
    tail = client.get("/discrepancies?limit=2&offset=10").json()
    assert tail["features"] == [] and tail["numberMatched"] == 4


def test_get_one_and_404(client, seed):
    """GET /discrepancies/{id} отдаёт Feature; несуществующий id -> 404."""
    resp = client.get(f"/discrepancies/{seed.d3}")
    assert resp.status_code == 200
    feat = resp.json()
    assert feat["type"] == "Feature" and feat["id"] == seed.d3
    assert feat["properties"]["iou"] == 0.3 and feat["properties"]["class"] == "geometry_mismatch"
    assert client.get("/discrepancies/999999").status_code == 404


def test_patch_status_updates_row_and_updated_at(client, seed, db_conn):
    """PATCH меняет статус и updated_at, возвращает обновлённый Feature; повторный PATCH возвращает в new."""
    before = client.get(f"/discrepancies/{seed.d1}").json()["properties"]
    resp = client.patch(f"/discrepancies/{seed.d1}", json={"status": "confirmed"})
    assert resp.status_code == 200, resp.text
    after = resp.json()["properties"]
    assert after["status"] == "confirmed"
    assert datetime.fromisoformat(after["updated_at"]) > datetime.fromisoformat(
        before["updated_at"]
    )
    row = db_conn.execute("SELECT status FROM discrepancies WHERE id = %s", [seed.d1]).fetchone()
    assert row["status"] == "confirmed"
    assert (
        client.patch(f"/discrepancies/{seed.d1}", json={"status": "new"}).json()["properties"][
            "status"
        ]
        == "new"
    )


def test_patch_invalid_and_missing(client, seed):
    """Невалидный статус -> 422 (строка не меняется), несуществующий id -> 404."""
    assert client.patch(f"/discrepancies/{seed.d2}", json={"status": "accepted"}).status_code == 422
    assert client.get(f"/discrepancies/{seed.d2}").json()["properties"]["status"] == "new"
    assert client.patch("/discrepancies/999999", json={"status": "rejected"}).status_code == 404


def test_export_contract_3_8(client, seed):
    """Экспорт: FeatureCollection с ровно тем набором properties, что в §3.8, и заголовком скачивания."""
    resp = client.get("/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    assert "attachment" in resp.headers["content-disposition"]
    fc = resp.json()
    assert fc["type"] == "FeatureCollection"
    assert ids(fc) == [seed.d1, seed.d4, seed.d2, seed.d3]
    for feat in fc["features"]:
        assert feat["type"] == "Feature"
        assert set(feat["properties"]) == EXPORT_KEYS
        assert feat["geometry"]["type"] == "MultiPolygon"
    d2 = next(f for f in fc["features"] if f["id"] == seed.d2)
    assert (
        d2["properties"]["ref_osm_id"] == 111
        and d2["properties"]["model_version"] == "test-00000000"
    )


def test_export_filters(client, seed):
    """Экспорт учитывает class, min_conf и status так же, как список."""
    assert ids(client.get("/export?class=missing_in_map&min_conf=0.8").json()) == [seed.d1]
    assert ids(client.get("/export?status=rejected").json()) == [seed.d4]
    assert ids(client.get(f"/export?bbox={AOI_BBOX_STR}&status=new").json()) == [seed.d1, seed.d2]


def test_get_one_with_scoring_features(client, seed, db_conn):
    """GET /discrepancies/{id} добавляет properties.features из discrepancy_features (D-012); без строки — null."""
    db_conn.execute(
        "INSERT INTO discrepancy_features (discrepancy_id, score, area_m2, area_norm, secondary_coverage,"
        " has_secondary, max_iou_any, aoi_edge, confidence_raw, confidence)"
        " VALUES (%s, 0.95, 120.5, 0.4, 0.8, true, 0.05, false, 0.87, 0.9)",
        [seed.d1],
    )
    db_conn.commit()
    feats = client.get(f"/discrepancies/{seed.d1}").json()["properties"]["features"]
    assert (
        feats["area_m2"] == 120.5 and feats["has_secondary"] is True and feats["aoi_edge"] is False
    )
    assert client.get(f"/discrepancies/{seed.d2}").json()["properties"]["features"] is None
    # list and export stay lightweight: no scoring block there
    listed = client.get(f"/discrepancies?bbox={AOI_BBOX_STR}").json()["features"][0]["properties"]
    assert "features" not in listed


def test_stats_counts(client, seed, db_conn):
    """/stats считает объекты по классам, статусам и их сочетаниям, перечисляет model_version и здания без NMS-подавленных."""
    det_c = db_conn.execute(
        "INSERT INTO buildings_det (geom, score, model_version) VALUES"
        " (ST_Multi(ST_GeomFromText('POLYGON((30.34 59.94, 30.3405 59.94, 30.3405 59.9405, 30.34 59.9405, 30.34 59.94))', 4326)),"
        " 0.5, 'test-00000000') RETURNING id"
    ).fetchone()["id"]
    db_conn.execute(
        "INSERT INTO det_suppressed (det_id, model_version, kept_det_id, iou) VALUES (%s, 'test-00000000', %s, 0.8)",
        [det_c, seed.det_a],
    )
    db_conn.commit()
    s = client.get("/stats").json()
    assert s["buildings"] == {"ref": 2, "det": 2, "det_suppressed": 1}
    assert s["total"] == 4
    assert s["by_class"] == {"missing_in_map": 2, "missing_on_image": 1, "geometry_mismatch": 1}
    assert s["by_status"] == {"new": 2, "confirmed": 1, "rejected": 1}
    assert s["by_class_status"]["missing_in_map"] == {"new": 1, "confirmed": 0, "rejected": 1}
    assert s["model_versions"] == ["test-00000000"]


def test_empty_queue(client, db_conn):
    """Пустая таблица: список и экспорт отдают пустые коллекции, статистика нули."""
    fc = client.get(f"/discrepancies?bbox={AOI_BBOX_STR}").json()
    assert fc["features"] == [] and fc["numberMatched"] == 0
    assert client.get("/export").json()["features"] == []
    assert client.get("/stats").json()["total"] == 0
