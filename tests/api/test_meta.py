"""/health, /config/map и прокси Esri: работа без файлов данных и без ключа."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from geoqc.api.main import create_app
from geoqc.api.routes import meta
from geoqc.db import get_dsn


@pytest.mark.db
def test_health_with_db(client):
    """При доступной БД /health отвечает {"status":"ok","db":true}."""
    assert client.get("/health").json() == {"status": "ok", "db": True}


def test_config_map_without_files(tmp_path):
    """Без aoi.geojson/manifest/COG API стартует: центр и bbox null, esri_enabled=false, note объясняет."""
    app = create_app(
        dsn=get_dsn(test=True),
        arcgis_api_key="",
        serve_static=False,
        paths={
            "aoi": tmp_path / "aoi.geojson",
            "manifest": tmp_path / "manifest.json",
            "cog": tmp_path / "cog.tif",
        },
    )
    with TestClient(app) as c:
        resp = c.get("/config/map")
    assert resp.status_code == 200
    cfg = resp.json()
    assert cfg["aoi"]["center"] is None and cfg["aoi"]["bbox"] is None
    assert cfg["esri_enabled"] is False and cfg["imagery_available"] is False
    assert cfg["attribution"]["esri"] is None
    assert "OpenStreetMap" in cfg["attribution"]["osm"] and "ODbL" in cfg["attribution"]["osm"]
    assert [b["key"] for b in cfg["basemaps"]] == ["osm"]
    assert "aoi.geojson" in cfg["note"] and "manifest.json" in cfg["note"]
    assert set(cfg["mvt_layers"]) == {"discrepancies", "buildings_ref", "buildings_det"}


def test_config_map_with_aoi_and_manifest(tmp_path):
    """С aoi.geojson и manifest.json отдаются центр/bbox AOI и атрибуция источника снимка."""
    aoi = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Test AOI"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [30.30, 59.93],
                            [30.36, 59.93],
                            [30.36, 59.96],
                            [30.30, 59.96],
                            [30.30, 59.93],
                        ]
                    ],
                },
            }
        ],
    }
    manifest = {
        "aoi": {
            "name": "Test AOI",
            "area_km2": 11.1,
            "utm_epsg": 32636,
            "bbox_wgs84": [30.30, 59.93, 30.36, 59.96],
        },
        "image": {
            "source": "openaerialmap",
            "id": "abc",
            "acquired": "2025-06-01",
            "license": "CC-BY-4.0",
            "attribution": "Some Pilot / OpenAerialMap",
            "gsd_m": 0.3,
            "crs": "EPSG:32636",
        },
        "osm": {"snapshot_date": "2026-09-01", "license": "ODbL"},
    }
    (tmp_path / "aoi.geojson").write_text(json.dumps(aoi), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    app = create_app(
        dsn=get_dsn(test=True),
        arcgis_api_key="",
        serve_static=False,
        paths={
            "aoi": tmp_path / "aoi.geojson",
            "manifest": tmp_path / "manifest.json",
            "cog": tmp_path / "cog.tif",
        },
    )
    with TestClient(app) as c:
        cfg = c.get("/config/map").json()
    assert cfg["aoi"]["name"] == "Test AOI"
    assert cfg["aoi"]["bbox"] == [30.3, 59.93, 30.36, 59.96]
    assert cfg["aoi"]["center"] == pytest.approx([30.33, 59.945], abs=1e-6)
    assert cfg["aoi"]["utm_epsg"] == 32636 and cfg["aoi"]["area_km2"] == 11.1
    assert cfg["attribution"]["image"] == "Some Pilot / OpenAerialMap (CC-BY-4.0)"
    assert cfg["image"]["source"] == "openaerialmap" and cfg["image"]["gsd_m"] == 0.3
    assert cfg["osm_snapshot_date"] == "2026-09-01"


def test_config_map_with_esri_key_never_leaks_it(monkeypatch):
    """С ключом esri_enabled=true и подложка ведёт на прокси; сам ключ в ответах отсутствует."""
    secret = "TOP-SECRET-ARCGIS-TOKEN"
    calls: list[dict] = []

    class FakeResponse:
        status_code = 200
        content = b"\xff\xd8jpegbytes"
        headers = {"Content-Type": "image/jpeg"}

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params})
        return FakeResponse()

    monkeypatch.setattr(meta.requests, "get", fake_get)
    app = create_app(dsn=get_dsn(test=True), arcgis_api_key=secret, serve_static=False)
    with TestClient(app) as c:
        cfg_resp = c.get("/config/map")
        tile = c.get("/basemap/esri/12/1300/2400")
    cfg = cfg_resp.json()
    assert cfg["esri_enabled"] is True
    assert secret not in cfg_resp.text
    esri = next(b for b in cfg["basemaps"] if b["key"] == "esri")
    assert esri["url"] == "/basemap/esri/{z}/{y}/{x}" and "Esri" in esri["attribution"]
    assert tile.status_code == 200 and tile.content == FakeResponse.content
    assert (
        tile.headers["content-type"] == "image/jpeg" and "max-age" in tile.headers["cache-control"]
    )
    assert secret not in tile.text
    assert calls and calls[0]["params"] == {"token": secret}
    assert calls[0]["url"].endswith("/tile/12/1300/2400")  # route and upstream are both z/y/x


def test_esri_proxy_upstream_error(monkeypatch):
    """Ошибка upstream Esri транслируется как 502, а не как 500."""

    class Bad:
        status_code = 403
        content = b""
        headers: dict = {}

    monkeypatch.setattr(meta.requests, "get", lambda *a, **k: Bad())
    app = create_app(dsn=get_dsn(test=True), arcgis_api_key="k", serve_static=False)
    with TestClient(app) as c:
        assert c.get("/basemap/esri/1/0/0").status_code == 502
