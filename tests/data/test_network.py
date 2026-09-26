"""Сетевые тесты (маркер network): реальные запросы к OpenAerialMap и Overpass."""

from __future__ import annotations

import pytest

from geoqc.data.aoi import fetch_oam_page, header_check
from geoqc.data.osm import fetch_overpass, overpass_bbox, polygonize_elements, snapshot_date_of

OVERPASS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


@pytest.mark.network
def test_oam_api_returns_results_with_bbox() -> None:
    """OAM API отвечает страницей с meta.found и записями, у которых есть bbox и gsd."""
    data = fetch_oam_page("https://api.openaerialmap.org/meta", page=1, limit=5, gsd_to=0.5)
    assert data["meta"]["found"] > 1000
    assert all("bbox" in r and "gsd" in r for r in data["results"])


@pytest.mark.network
def test_remote_header_of_chosen_image_is_rgb_cog() -> None:
    """Выбранный снимок открывается по /vsicurl/ как RGB uint8 в проекции UTM."""
    url = "https://oin-hotosm-temp.s3.amazonaws.com/5f3f96b8a646430007b2ac8f/0/5f3f96b8a646430007b2ac90.tif"
    info = header_check(url)
    assert info["header_ok"] and info["bands"] == 3 and info["crs"] == "EPSG:32636"


@pytest.mark.network
@pytest.mark.slow
def test_overpass_small_bbox_polygonizes() -> None:
    """Overpass возвращает здания в маленьком bbox СПб, ответ полигонизируется."""
    bbox = overpass_bbox((30.40, 59.885, 30.405, 59.888))
    q = f'[out:json][timeout:60]; (way["building"]({bbox}); relation["building"]({bbox});); out geom;'
    data = fetch_overpass(q, OVERPASS, timeout_s=120, retries=2, backoff_s=2)
    feats = polygonize_elements(data["elements"], snapshot_date=snapshot_date_of(data))
    assert len(feats) > 5
