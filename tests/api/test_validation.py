"""Валидация параметров запросов: ошибки должны возвращать 422 до обращения к БД."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "query",
    [
        "bbox=1,2,3",
        "bbox=a,b,c,d",
        "bbox=30.4,59.9,30.3,60.0",
        "bbox=-200,0,10,10",
        "class=foo",
        "class=missing_in_map,bogus",
        "status=maybe",
        "min_conf=1.5",
        "min_conf=-0.1",
        "limit=0",
        "limit=1000000",
        "offset=-1",
    ],
)
def test_list_rejects_invalid_query(client, query):
    """Некорректные bbox/class/status/min_conf/limit/offset дают 422 с указанием параметра."""
    resp = client.get(f"/discrepancies?{query}")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail and "loc" in detail[0]


@pytest.mark.parametrize("query", ["class=nope", "status=nope", "min_conf=2", "bbox=1,2"])
def test_export_rejects_invalid_query(client, query):
    """Экспорт использует те же правила валидации фильтров, что и список."""
    assert client.get(f"/export?{query}").status_code == 422


@pytest.mark.parametrize("body", [{"status": "maybe"}, {}, {"status": 1}, {"state": "new"}])
def test_patch_rejects_invalid_status(client, body):
    """PATCH принимает только new/confirmed/rejected в поле status."""
    resp = client.patch("/discrepancies/1", json=body)
    assert resp.status_code == 422


def test_patch_rejects_non_integer_id(client):
    """Нечисловой идентификатор в пути даёт 422."""
    assert client.patch("/discrepancies/abc", json={"status": "new"}).status_code == 422
    assert client.get("/discrepancies/abc").status_code == 422


@pytest.mark.parametrize(
    "path",
    ["/tiles/30/0/0.mvt", "/tiles/3/8/0.mvt", "/tiles/3/0/-1.mvt", "/tiles/2/1/1.mvt?layers=nope"],
)
def test_tiles_reject_bad_coordinates(client, path):
    """Тайлы вне диапазона z/x/y или с неизвестным слоем отклоняются с 422."""
    assert client.get(path).status_code == 422


@pytest.mark.parametrize("path", ["/imagery/30/0/0.png", "/imagery/2/4/0.png"])
def test_imagery_rejects_bad_coordinates(client, path):
    """Тайлы снимка вне диапазона z/x/y отклоняются с 422 ещё до чтения COG."""
    assert client.get(path).status_code == 422


def test_esri_proxy_disabled_without_key(client):
    """Без ARCGIS_API_KEY прокси Esri отвечает 404, ключ клиенту не нужен и не отдаётся."""
    resp = client.get("/basemap/esri/1/0/0")
    assert resp.status_code == 404
    assert "ARCGIS_API_KEY" in resp.json()["detail"]
