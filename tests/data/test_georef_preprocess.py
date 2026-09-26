"""Офлайн-проверки georef (G1) и preprocess на синтетических растрах с известным сдвигом."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from geoqc.data.georef import offset_for_building, select_controls
from geoqc.data.preprocess import build_aoi_cog

EPSG = 32637
X0, Y0 = 500000.0, 6000000.0


def _raster(path, res, size, rects, background=30):
    """RGB uint8 raster in UTM with bright rectangles (metric coordinates)."""
    tr = from_origin(X0, Y0, res, res)
    img = np.full((3, size, size), background, dtype=np.uint8)
    inv = ~tr
    for minx, miny, maxx, maxy in rects:
        c0, r0 = inv * (minx, maxy)
        c1, r1 = inv * (maxx, miny)
        img[:, int(round(r0)) : int(round(r1)), int(round(c0)) : int(round(c1))] = 230
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=3, dtype="uint8",
        crs=f"EPSG:{EPSG}", transform=tr, nodata=0,
    ) as dst:  # fmt: skip
        dst.write(img)
    return path


BUILDING = (X0 + 20.0, Y0 - 40.0, X0 + 40.0, Y0 - 25.0)  # 20 x 15 m


@pytest.mark.parametrize("shift", [(0.0, 0.0), (1.5, -0.9), (-2.1, 1.2)])
def test_offset_recovers_known_shift(tmp_path, shift):
    """Смещение снимка относительно контура OSM восстанавливается с точностью до пикселя, знаки восток/север."""
    dx, dy = shift
    minx, miny, maxx, maxy = BUILDING
    img = _raster(tmp_path / "img.tif", 0.3, 220, [(minx + dx, miny + dy, maxx + dx, maxy + dy)])
    with rasterio.open(img) as ds:
        res = offset_for_building(ds, box(*BUILDING), search_radius_m=4.0)
    assert res is not None
    assert res["dx_m"] == pytest.approx(dx, abs=0.3)
    assert res["dy_m"] == pytest.approx(dy, abs=0.3)
    assert res["dist_m"] == pytest.approx(np.hypot(dx, dy), abs=0.4)


def test_offset_none_on_nodata_window(tmp_path):
    """Окно с nodata (край снимка) не даёт контрольной точки."""
    img = _raster(tmp_path / "img.tif", 0.3, 220, [BUILDING], background=0)
    with rasterio.open(img) as ds:
        assert offset_for_building(ds, box(*BUILDING), search_radius_m=4.0) is None


def test_select_controls_filters():
    """Контрольные здания: площадь, компактность, изоляция, этажность, внутри AOI."""
    cfg = {
        "min_area_m2": 50, "max_area_m2": 2000, "min_rectangularity": 0.8, "search_radius_m": 4.0,
        "isolation_m": 5.0, "max_levels": 3,
    }  # fmt: skip
    ok = box(100, 100, 120, 115)
    small = box(200, 200, 205, 205)
    tall = box(300, 100, 320, 115)
    near_a, near_b = box(100, 300, 120, 315), box(122, 300, 140, 315)
    l_shape = box(400, 100, 440, 140).difference(box(405, 105, 440, 140))
    outside = box(990, 990, 1010, 1010)
    gdf = gpd.GeoDataFrame(
        {
            "name": ["ok", "small", "tall", "near_a", "near_b", "l_shape", "outside"],
            "tags": [{}, {}, {"building:levels": "9"}, {}, {}, {}, {}],
        },
        geometry=[ok, small, tall, near_a, near_b, l_shape, outside],
        crs=f"EPSG:{EPSG}",
    )
    got = select_controls(gdf, box(0, 0, 1000, 1000), cfg)
    assert got["name"].tolist() == ["ok"]


def test_build_aoi_cog_shift_and_mask(tmp_path):
    """preprocess: сетка кратна GSD, сдвиг (dx, dy) переносит содержимое на восток/север, вне AOI — nodata."""
    square = (X0 + 20.0, Y0 - 40.0, X0 + 30.0, Y0 - 30.0)
    src = _raster(tmp_path / "src.tif", 0.15, 480, [square])  # 72 x 72 m
    # L-shaped AOI: the north-east quadrant of the bbox is outside
    aoi_m = box(X0 + 5.0, Y0 - 65.0, X0 + 60.0, Y0 - 5.0).difference(
        box(X0 + 45.0, Y0 - 20.0, X0 + 70.0, Y0)
    )
    aoi_wgs = gpd.GeoSeries([aoi_m], crs=f"EPSG:{EPSG}").to_crs(4326).iloc[0]

    def centroid(path):
        with rasterio.open(path) as ds:
            band = ds.read(1)
            rows, cols = np.nonzero(band > 200)
            x, y = ds.transform * (cols.mean() + 0.5, rows.mean() + 0.5)
            return x, y, band

    base = build_aoi_cog(
        src, tmp_path / "a.tif", aoi_wgs84=aoi_wgs, dst_epsg=EPSG, target_gsd_m=0.3
    )
    shifted = build_aoi_cog(
        src, tmp_path / "b.tif", aoi_wgs84=aoi_wgs, dst_epsg=EPSG, target_gsd_m=0.3,
        shift_m=(2.0, -1.0),
    )  # fmt: skip
    assert base["crs"] == f"EPSG:{EPSG}" and base["resampling"] == "average"
    minx = base["bounds"][0]
    assert minx / 0.3 == pytest.approx(round(minx / 0.3), abs=1e-6)
    assert shifted["shift_applied_m"] == [2.0, -1.0]
    x0, y0, _ = centroid(tmp_path / "a.tif")
    assert (x0, y0) == pytest.approx((X0 + 25.0, Y0 - 35.0), abs=0.3)
    x1, y1, _ = centroid(tmp_path / "b.tif")
    assert (x1 - x0, y1 - y0) == pytest.approx((2.0, -1.0), abs=0.3)
    with rasterio.open(tmp_path / "a.tif") as ds:
        assert ds.crs.to_epsg() == EPSG
        band = ds.read(1)
        assert band[ds.index(X0 + 55.0, Y0 - 10.0)] == 0  # outside the AOI
        assert band[ds.index(X0 + 10.0, Y0 - 10.0)] == 30  # inside, background
