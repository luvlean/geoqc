"""Детекция на загруженном снимке: приведение к 0,3 м, тайлы с перекрытием, склейка контуров, координаты."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from geoqc.cv import image_detect
from geoqc.cv.predictor import MockPredictor

VEC = {
    "simplify_tolerance_m": 0.0,
    "min_area_m2": 12,
    "tile_overlap_merge_iou": 0.1,
    "min_overlap_ratio": 0.5,
}


def _scene(h: int = 900, w: int = 1100) -> np.ndarray:
    """Dark image with two bright roofs; the long one crosses the tile edge at column 448."""
    img = np.full((h, w, 3), 40, np.uint8)
    img[100:160, 400:560] = 255  # 60 x 160 px = 864 m2 at 0.3 m, cut by the tile seam
    img[600:640, 800:840] = 230  # 40 x 40 px = 144 m2
    return img


def _write_geotiff(path, img: np.ndarray, gsd: float) -> None:
    h, w, _ = img.shape
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=w,
        height=h,
        count=3,
        dtype="uint8",
        crs="EPSG:32636",
        transform=from_origin(300000, 6620000, gsd, gsd),
    ) as dst:
        dst.write(np.transpose(img, (2, 0, 1)))


def test_png_tiles_are_merged_into_whole_buildings(tmp_path):
    """PNG без привязки при 0,3 м: здание на стыке тайлов — один контур, площади в м², гео нет."""
    p = tmp_path / "scene.png"
    Image.fromarray(_scene()).save(p)
    img = image_detect.load_image(p)
    assert (img.gsd_from, img.transform) == ("default", None)
    res = image_detect.detect(img, MockPredictor(threshold=200), VEC, conf=0.5)
    assert res.n_tiles == 6  # rows 0, 388; columns 0, 448, 588
    pixel, wgs84 = image_detect.to_feature_collections(res)
    areas = sorted(f["properties"]["area_m2"] for f in pixel["features"])
    assert areas == pytest.approx([144.0, 864.0], rel=0.02)
    assert wgs84 is None
    minx, miny, maxx, maxy = image_detect.as_multipolygon(res.detections[0].geom).bounds
    assert minx >= 0 and maxx <= 1100 and miny >= 0 and maxy <= 900  # preview pixels


def test_geotiff_is_resampled_to_target_gsd_and_georeferenced(tmp_path):
    """GeoTIFF 0,15 м сжимается вдвое до 0,3 м; площади те же, контуры получают координаты WGS 84."""
    p = tmp_path / "scene.tif"
    _write_geotiff(p, _upsampled(), 0.15)
    img = image_detect.load_image(p)
    assert img.gsd_from == "georeference" and img.source_gsd_m == pytest.approx(0.15)
    assert img.rgb.shape[:2] == (900, 1100)
    res = image_detect.detect(img, MockPredictor(threshold=200), VEC, conf=0.5)
    pixel, wgs84 = image_detect.to_feature_collections(res)
    assert sorted(f["properties"]["area_m2"] for f in pixel["features"]) == pytest.approx(
        [144.0, 864.0], rel=0.05
    )
    with rasterio.open(p) as src:
        west, south, east, north = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    lon, lat = wgs84["features"][0]["geometry"]["coordinates"][0][0][0]
    assert west < lon < east and south < lat < north


def _upsampled() -> np.ndarray:
    """The 0.3 m scene drawn at 0.15 m (twice the pixels)."""
    return np.repeat(np.repeat(_scene(), 2, axis=0), 2, axis=1)


def test_user_gsd_overrides_and_size_limit(tmp_path):
    """Заданный размер пикселя важнее привязки; слишком большой результат — понятная ошибка."""
    p = tmp_path / "scene.png"
    Image.fromarray(_scene(300, 400)).save(p)
    img = image_detect.load_image(p, gsd_m=0.6)
    assert img.gsd_from == "user" and img.rgb.shape[:2] == (600, 800)
    with pytest.raises(image_detect.ImageError, match="больше лимита"):
        image_detect.load_image(p, gsd_m=3.0, max_side_px=2000)


def test_conf_threshold_and_non_raster(tmp_path):
    """Порог отбрасывает тёмную крышу (score MockPredictor растёт с яркостью); не растр — ImageError."""
    p = tmp_path / "scene.png"
    Image.fromarray(_scene()).save(p)
    img = image_detect.load_image(p)
    res = image_detect.detect(img, MockPredictor(threshold=200), VEC, conf=0.97)
    assert len(res.detections) == 1  # 255 -> score 1.0 stays, 230 -> 0.95 is dropped
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    with pytest.raises(image_detect.ImageError):
        image_detect.load_image(bad)
    assert image_detect.preview_data_url(img).startswith("data:image/jpeg;base64,")


def test_geographic_crs_keeps_buildings_square(tmp_path):
    """EPSG:4326 на широте 60°: пиксель 0,3 × 0,3 м — это неравные градусы; площадь здания не искажается."""
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    lon0, lat0 = 30.0, 60.0
    dx = geod.fwd(lon0, lat0, 90, 0.3)[0] - lon0  # degrees for 0.3 m east
    dy = geod.fwd(lon0, lat0, 0, 0.3)[1] - lat0  # degrees for 0.3 m north
    img = np.full((600, 600, 3), 40, np.uint8)
    img[200:300, 200:300] = 255  # 100 x 100 px = 30 x 30 m
    p = tmp_path / "wgs84.tif"
    with rasterio.open(
        p,
        "w",
        driver="GTiff",
        width=600,
        height=600,
        count=3,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(lon0, lat0 + 300 * dy, dx, dy),
    ) as dst:
        dst.write(np.transpose(img, (2, 0, 1)))
    loaded = image_detect.load_image(p)
    assert loaded.rgb.shape[:2] == (600, 600) and loaded.source_gsd_m == pytest.approx(
        0.3, rel=0.01
    )
    res = image_detect.detect(loaded, MockPredictor(threshold=200), VEC, conf=0.5)
    pixel, _ = image_detect.to_feature_collections(res)
    assert [f["properties"]["area_m2"] for f in pixel["features"]] == pytest.approx(
        [900.0], rel=0.02
    )


def test_palette_png_uses_colours_not_indices(tmp_path):
    """Индексированный PNG: в канале номера цветов, снимок собирается из палитры."""
    img = np.full((300, 300, 3), 40, np.uint8)
    img[100:160, 100:160] = 255  # 60 x 60 px = 324 m2
    p = tmp_path / "pal.png"
    Image.fromarray(img).convert("P", palette=Image.ADAPTIVE, colors=4).save(p)
    loaded = image_detect.load_image(p)
    assert loaded.rgb.max() == 255
    res = image_detect.detect(loaded, MockPredictor(threshold=200), VEC, conf=0.5)
    assert [round(d.geom.area * 0.09) for d in res.detections] == [324]
