"""Тесты тайлинга синтетического растра (docs/CONTRACTS.md §3.3)."""

from __future__ import annotations

import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio

from geoqc.data.tiling import grid_positions, tile_raster


@pytest.mark.parametrize(
    ("length", "size", "overlap", "expected"),
    [
        (512, 512, 64, [0]),
        (513, 512, 64, [0, 448]),
        (1200, 512, 64, [0, 448, 896]),
        (900, 512, 64, [0, 448]),
        (100, 512, 64, [0]),
    ],
)
def test_grid_positions(length: int, size: int, overlap: int, expected: list[int]) -> None:
    """Смещения окон кратны шагу (size - overlap) и полностью покрывают длину."""
    pos = grid_positions(length, size, overlap)
    assert pos == expected
    assert pos[-1] + size >= length


def test_grid_positions_rejects_bad_overlap() -> None:
    """Перекрытие не меньше размера тайла недопустимо."""
    with pytest.raises(ValueError):
        grid_positions(1000, 512, 512)


def test_tile_raster_counts_overlap_size_and_index(synthetic_cog: Path, tmp_path: Path) -> None:
    """На растре 1200x900 получаем 3x2 тайла 512x512 с перекрытием 64 px, nodata-заполнением и индексом GPKG."""
    out_dir = tmp_path / "data" / "tiles"
    index = out_dir / "tiles.gpkg"
    res = tile_raster(synthetic_cog, out_dir, index, size_px=512, overlap_px=64, nodata=0)
    assert res["n_rows"] == 2 and res["n_cols"] == 3
    assert res["n_tiles"] == 6
    tifs = sorted(out_dir.glob("*.tif"))
    assert len(tifs) == 6
    assert {p.name for p in tifs} == {f"{r}_{c}.tif" for r in range(2) for c in range(3)}
    with rasterio.open(out_dir / "0_0.tif") as a, rasterio.open(out_dir / "0_1.tif") as b:
        assert a.width == a.height == 512 and a.count == 3 and a.dtypes[0] == "uint8"
        assert a.nodata == 0
        # Overlap: tile 0_1 starts 448 px (= 512 - 64) to the right of tile 0_0.
        assert math.isclose(b.transform.c - a.transform.c, 448 * 0.3, rel_tol=1e-9)
        # The same source column is read in both tiles inside the overlap band.
        band_a = a.read(1)[:, 448:512]
        band_b = b.read(1)[:, 0:64]
        assert np.array_equal(band_a, band_b)
    with rasterio.open(out_dir / "1_2.tif") as edge:
        arr = edge.read(1)
        # Source is 900 px tall: rows beyond 900 - 448 = 452 are padded with nodata.
        assert np.all(arr[452:, :] == 0)
        # Source is 1200 px wide: cols beyond 1200 - 896 = 304 are padded with nodata.
        assert np.all(arr[:, 304:] == 0)
    gdf = gpd.read_file(index, layer="tiles")
    assert list(gdf.columns) == ["tile_id", "row", "col", "path", "geometry"]
    assert gdf.crs.to_epsg() == 4326
    assert set(gdf["tile_id"]) == {f"{r}_{c}" for r in range(2) for c in range(3)}
    assert gdf.geometry.is_valid.all()


def test_tile_raster_respects_aoi_and_coverage(
    synthetic_cog: Path, synthetic_aoi_native, tmp_path: Path
) -> None:
    """Тайлы вне AOI пропускаются, покрытие AOI оставшимися тайлами полное."""
    from shapely.geometry import box

    out_dir = tmp_path / "tiles"
    small_aoi = box(355000.0, 6642000.0 - 100, 355000.0 + 100, 6642000.0)  # top-left corner only
    res = tile_raster(synthetic_cog, out_dir, out_dir / "tiles.gpkg", aoi_geom=small_aoi)
    assert res["n_tiles"] == 1
    assert res["n_skipped_outside_aoi"] == 5
    assert res["aoi_coverage"] == pytest.approx(1.0)
    res_full = tile_raster(
        synthetic_cog,
        tmp_path / "t2",
        tmp_path / "t2" / "tiles.gpkg",
        aoi_geom=synthetic_aoi_native,
    )
    assert res_full["n_tiles"] == 6
    assert res_full["aoi_coverage"] == pytest.approx(1.0)
