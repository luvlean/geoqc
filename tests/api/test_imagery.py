"""Тайлы снимка из COG через rio-tiler: 404 без файла, PNG внутри покрытия, прозрачный PNG вне."""

from __future__ import annotations

import morecantile
import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from geoqc.api.main import create_app
from geoqc.db import get_dsn

TMS = morecantile.tms.get("WebMercatorQuad")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def cog_path(tmp_path_factory):
    """Синтетический RGB GeoTIFF 256x256 px, 0.5 м/px, EPSG:32636 (UTM 36N), около 30.31E 59.94N."""
    path = tmp_path_factory.mktemp("cog") / "aoi_cog.tif"
    # 30.31E 59.94N -> EPSG:32636 ~ (350 460, 6 646 060); place the raster there.
    transform = from_origin(350400.0, 6646130.0, 0.5, 0.5)
    data = np.zeros((3, 256, 256), dtype="uint8")
    data[0] = 200
    data[1] = 120
    data[2] = 40
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=256,
        height=256,
        count=3,
        dtype="uint8",
        crs="EPSG:32636",
        transform=transform,
        nodata=0,
        tiled=True,
        blockxsize=128,
        blockysize=128,
    ) as dst:
        dst.write(data)
    return path


@pytest.fixture(scope="module")
def imagery_client(cog_path):
    app = create_app(
        dsn=get_dsn(test=True), arcgis_api_key="", serve_static=False, paths={"cog": cog_path}
    )
    with TestClient(app) as c:
        yield c


def test_imagery_404_when_cog_missing(tmp_path):
    """Без data/interim/aoi_cog.tif любой тайл снимка отвечает 404 с понятным сообщением."""
    app = create_app(
        dsn=get_dsn(test=True), serve_static=False, paths={"cog": tmp_path / "nope.tif"}
    )
    with TestClient(app) as c:
        resp = c.get("/imagery/16/36000/20000.png")
    assert resp.status_code == 404
    assert "imagery not available" in resp.json()["detail"]


def test_imagery_tile_inside_coverage(imagery_client, cog_path):
    """Тайл над растром — непустой PNG 256x256 с ненулевыми пикселями и Cache-Control."""
    with rasterio.open(cog_path) as src:
        lon, lat = rasterio.warp.transform(
            src.crs, "EPSG:4326", [src.bounds.left + 60], [src.bounds.top - 60]
        )
    t = TMS.tile(lon[0], lat[0], 19)
    resp = imagery_client.get(f"/imagery/{t.z}/{t.x}/{t.y}.png")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "public, max-age=86400"
    assert resp.content.startswith(PNG_SIGNATURE)
    with MemoryFile(resp.content) as mf, mf.open() as png:
        arr = png.read()
    assert arr.shape == (4, 256, 256)
    assert arr[0].max() > 0 and arr[3].max() == 255


def test_imagery_tile_outside_coverage_is_transparent(imagery_client):
    """Тайл вне покрытия COG — полностью прозрачный PNG, а не ошибка."""
    resp = imagery_client.get("/imagery/10/512/512.png")
    assert resp.status_code == 200
    assert resp.content.startswith(PNG_SIGNATURE)
    with MemoryFile(resp.content) as mf, mf.open() as png:
        arr = png.read()
    assert arr.shape[0] == 4 and arr[3].max() == 0
