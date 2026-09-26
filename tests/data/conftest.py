"""Fixtures for the data module tests: synthetic rasters and geometries."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def synthetic_cog(tmp_path: Path) -> Path:
    """RGB uint8 GeoTIFF 1200x900 px @ 0.3 m in EPSG:32636 with a nodata border."""
    w, h, res = 1200, 900, 0.3
    transform = from_origin(355000.0, 6642000.0, res, res)
    rng = np.random.default_rng(42)
    data = rng.integers(20, 235, size=(3, h, w), dtype=np.uint8)
    data[:, :, :40] = 0  # nodata column strip on the left
    path = tmp_path / "aoi_cog.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=w,
        height=h,
        count=3,
        dtype="uint8",
        crs="EPSG:32636",
        transform=transform,
        nodata=0,
        tiled=True,
        blockxsize=256,
        blockysize=256,
    ) as dst:
        dst.write(data)
    return path


@pytest.fixture
def synthetic_aoi_native():
    """AOI covering the whole synthetic raster extent (EPSG:32636)."""
    return box(355000.0, 6642000.0 - 900 * 0.3, 355000.0 + 1200 * 0.3, 6642000.0)
