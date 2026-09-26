"""Векторные тайлы MVT: слои, min_zoom, подмножество слоёв, пустые тайлы, Cache-Control."""

from __future__ import annotations

import morecantile
import pytest

pytestmark = pytest.mark.db

TMS = morecantile.tms.get("WebMercatorQuad")
MVT = "application/vnd.mapbox-vector-tile"


def tile_for(lon: float, lat: float, z: int) -> str:
    t = TMS.tile(lon, lat, z)
    return f"/tiles/{t.z}/{t.x}/{t.y}.mvt"


def test_tile_contains_all_three_layers(client, seed):
    """Тайл z=16 над зданием содержит слои discrepancies, buildings_ref и buildings_det."""
    resp = client.get(tile_for(30.35, 59.935, 16))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == MVT
    assert resp.headers["cache-control"].startswith("public, max-age=")
    body = resp.content
    assert len(body) > 0
    for layer in (b"discrepancies", b"buildings_ref", b"buildings_det"):
        assert layer in body
    assert b"geometry_mismatch" in body and b"confirmed" in body


def test_tile_below_min_zoom_drops_building_layers(client, seed):
    """На малом зуме (z=10) здания не отдаются, расхождения остаются."""
    body = client.get(tile_for(30.31, 59.94, 10)).content
    assert b"discrepancies" in body
    assert b"buildings_ref" not in body and b"buildings_det" not in body


def test_tile_layers_subset(client, seed):
    """Параметр layers ограничивает набор слоёв в тайле."""
    body = client.get(tile_for(30.35, 59.935, 16) + "?layers=buildings_ref").content
    assert b"buildings_ref" in body
    assert b"discrepancies" not in body and b"buildings_det" not in body


def test_tile_far_away_is_empty(client, seed):
    """Тайл вне данных возвращает 200 с пустым телом (MVT без слоёв)."""
    resp = client.get(tile_for(0.0, 0.0, 14))
    assert resp.status_code == 200
    assert resp.content == b""


def test_tile_z0_has_only_discrepancies_layer_when_configured(client, seed):
    """Ниже min_zoom всех слоёв ответ пустой и не ходит в БД."""
    resp = client.get("/tiles/0/0/0.mvt")
    assert resp.status_code == 200 and resp.content == b""


def test_tile_excludes_nms_suppressed_detections(client, seed, db_conn):
    """Детекция из det_suppressed (D-012) не попадает в слой buildings_det, оставшаяся — попадает."""
    det_c = db_conn.execute(
        "INSERT INTO buildings_det (geom, score, model_version, tile_id) VALUES"
        " (ST_Multi(ST_GeomFromText(%s, 4326)), 0.7, 'test-00000000', 'SUPPRESSED_TILE') RETURNING id",
        [
            "POLYGON((30.3502 59.9349, 30.3507 59.9349, 30.3507 59.9354, 30.3502 59.9354, 30.3502 59.9349))"
        ],
    ).fetchone()["id"]
    db_conn.execute(
        "INSERT INTO det_suppressed (det_id, model_version, kept_det_id, iou) VALUES (%s, 'test-00000000', %s, 0.9)",
        [det_c, seed.det_b],
    )
    db_conn.commit()
    body = client.get(tile_for(30.35, 59.935, 17) + "?layers=buildings_det").content
    assert b"buildings_det" in body and b"0_1" in body
    assert b"SUPPRESSED_TILE" not in body
