"""Векторизация масок: полигоны, площади, CRS, упрощение, фильтр по площади."""

from __future__ import annotations

import numpy as np
from rasterio.transform import from_origin
from shapely.geometry import MultiPolygon

from geoqc.cv.predictor import Mask
from geoqc.cv.vectorize import (
    Detection,
    as_multipolygon,
    filter_min_area,
    mask_to_geometry,
    simplify_detections,
    to_geodataframe,
    vectorize_masks,
)

T = from_origin(500000.0, 6000000.0, 0.5, 0.5)  # 0.5 m pixels, UTM 37N


def test_two_squares_become_two_polygons_with_metric_area():
    """Маска с двумя квадратами 20х20 и 10х10 px -> два полигона площадью 100 и 25 м²."""
    mask = np.zeros((128, 128), dtype=bool)
    mask[10:30, 10:30] = True
    mask[60:70, 80:90] = True
    geom = mask_to_geometry(mask, T)
    assert isinstance(geom, MultiPolygon)
    areas = sorted(round(g.area, 6) for g in geom.geoms)
    assert areas == [25.0, 100.0]
    assert geom.is_valid


def test_vectorize_masks_keeps_score_and_tile_and_respects_nodata():
    """vectorize_masks переносит score/tile_id и вырезает nodata-пиксели из маски."""
    arr = np.zeros((64, 64), dtype=bool)
    arr[0:20, 0:20] = True
    valid = np.ones((64, 64), dtype=bool)
    valid[:, 0:10] = False  # left 10 columns are nodata padding
    dets = vectorize_masks([Mask(arr, 0.8)], T, "3_4", valid_mask=valid)
    assert len(dets) == 1
    d = dets[0]
    assert d.tile_id == "3_4" and d.score == 0.8
    assert abs(d.area - 20 * 10 * 0.25) < 1e-6  # 20 rows x 10 remaining cols x 0.25 m²


def test_to_geodataframe_outputs_multipolygon_in_wgs84():
    """Результат to_geodataframe — EPSG:4326, MultiPolygon и обязательные свойства §3.6."""
    mask = np.zeros((32, 32), dtype=bool)
    mask[4:14, 4:14] = True
    dets = vectorize_masks([Mask(mask, 0.9)], T, "0_0")
    gdf = to_geodataframe(dets, "EPSG:32637", "mock-00000000")
    assert gdf.crs.to_epsg() == 4326
    assert list(gdf.columns) == ["geometry", "score", "model_version", "tile_id"] or set(
        gdf.columns
    ) == {"geometry", "score", "model_version", "tile_id"}
    assert gdf.geom_type.unique().tolist() == ["MultiPolygon"]
    lon, lat = gdf.geometry.iloc[0].centroid.x, gdf.geometry.iloc[0].centroid.y
    assert 38 < lon < 40 and 54 < lat < 55  # UTM zone 37N, ~54.1N
    assert 0 <= gdf["score"].iloc[0] <= 1


def test_min_area_filter_drops_small_polygons():
    """Фильтр min_area_m2 отбрасывает пятно 2х2 px (1 м²), оставляет квадрат 100 м²."""
    mask = np.zeros((64, 64), dtype=bool)
    mask[0:20, 0:20] = True
    mask[40:42, 40:42] = True
    dets = [Detection(g, 0.5, "0_0") for g in mask_to_geometry(mask, T).geoms]
    assert len(dets) == 2
    kept = filter_min_area(dets, 12.0)
    assert len(kept) == 1 and abs(kept[0].area - 100.0) < 1e-6


def test_simplify_reduces_vertices_and_keeps_validity():
    """Дуглас-Пекер с tolerance 0.5 м убирает лестницу пикселей на диагонали, геометрия валидна."""
    mask = np.zeros((64, 64), dtype=bool)
    for i in range(40):
        mask[i, 10 : 10 + i + 1] = True  # right triangle with a staircase hypotenuse
    d = Detection(mask_to_geometry(mask, T), 0.7, "0_0")
    raw_vertices = len(d.geom.exterior.coords)
    simplified = simplify_detections([d], 0.5)[0]
    assert simplified.geom.is_valid
    assert len(simplified.geom.exterior.coords) < raw_vertices
    assert abs(simplified.geom.area - d.geom.area) / d.geom.area < 0.05


def test_as_multipolygon_repairs_invalid_geometry():
    """as_multipolygon чинит самопересекающийся полигон (бабочку) и возвращает MultiPolygon."""
    from shapely.geometry import Polygon

    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
    assert not bowtie.is_valid
    fixed = as_multipolygon(bowtie)
    assert isinstance(fixed, MultiPolygon) and fixed.is_valid and fixed.area > 0
