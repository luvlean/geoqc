"""Reproject/clip/resample the source image into the AOI COG and keep pipeline.yaml in sync.

Output contract (docs/CONTRACTS.md §3.2): ``data/interim/aoi_cog.tif`` — COG, RGB uint8, UTM
metric CRS, clipped by ``config/aoi.geojson``, GSD <= 0.5 m. An optional ``shift=(dx, dy)`` in
metres translates the georeference (georef correction, manifest ``georef.shift_applied_m``).
"""

from __future__ import annotations

import logging
import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio import features
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles
from ruamel.yaml import YAML
from shapely.geometry.base import BaseGeometry

from geoqc.config import CONFIG_DIR
from geoqc.data.tiling import aoi_to_crs

log = logging.getLogger(__name__)


def set_pipeline_metric_crs(epsg: int, pipeline_yaml: Path | None = None) -> Path:
    """Round-trip edit of exactly ``crs.metric`` in ``config/pipeline.yaml`` (comments preserved)."""
    path = pipeline_yaml or (CONFIG_DIR / "pipeline.yaml")
    yaml = YAML()
    yaml.preserve_quotes = True
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.load(fh)
    doc["crs"]["metric"] = f"EPSG:{int(epsg)}"
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return path


def _pick_resampling(src_res: float, dst_res: float, downsampling: str) -> Resampling:
    if dst_res > src_res * 1.05:
        return Resampling[downsampling]
    return Resampling.cubic


def build_aoi_cog(
    src_path: Path,
    out_path: Path,
    *,
    aoi_wgs84: BaseGeometry,
    dst_epsg: int,
    target_gsd_m: float,
    nodata: int = 0,
    resampling: str = "average",
    shift_m: tuple[float, float] = (0.0, 0.0),
    block_px: int = 2048,
    cog_profile: str = "deflate",
) -> dict[str, Any]:
    """Warp ``src_path`` to UTM at ``target_gsd_m``, mask outside the AOI and write a COG."""
    dst_crs = rasterio.crs.CRS.from_epsg(dst_epsg)
    aoi_dst = aoi_to_crs(aoi_wgs84, dst_crs)
    minx, miny, maxx, maxy = aoi_dst.bounds
    # Snap the grid to whole GSD multiples so tiles align across re-runs.
    minx = math.floor(minx / target_gsd_m) * target_gsd_m
    maxy = math.ceil(maxy / target_gsd_m) * target_gsd_m
    width = int(math.ceil((maxx - minx) / target_gsd_m))
    height = int(math.ceil((maxy - miny) / target_gsd_m))
    dst_transform = Affine(target_gsd_m, 0.0, minx, 0.0, -target_gsd_m, maxy)
    dx, dy = shift_m
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src_path) as src:
        if src.count < 3:
            raise ValueError(f"{src_path} has {src.count} band(s); RGB expected")
        src_res = float(abs(src.res[0]))
        # Compare in metres: approximate degrees->metres for geographic sources.
        if src.crs != dst_crs and src.crs and src.crs.is_geographic:
            src_res = src_res * 111_320.0 * math.cos(math.radians(aoi_wgs84.centroid.y))
        rs = _pick_resampling(src_res, target_gsd_m, resampling)
        # When the image must be shifted by (dx, dy) metres (east, north), the output pixel at
        # (x, y) must show source content from (x - dx, y - dy). Rows grow southwards, so the
        # pixel-space translation is (-dx / gsd, +dy / gsd).
        warp_transform = dst_transform * Affine.translation(-dx / target_gsd_m, dy / target_gsd_m)
        vrt_opts = {
            "crs": dst_crs,
            "transform": warp_transform,
            "width": width,
            "height": height,
            "resampling": rs,
            "nodata": nodata,
            "src_nodata": src.nodata,
            "add_alpha": False,
            "dtype": "uint8",
        }
        with tempfile.TemporaryDirectory(dir=out_path.parent) as tmpdir:
            tmp = Path(tmpdir) / "warped.tif"
            profile = {
                "driver": "GTiff",
                "width": width,
                "height": height,
                "count": 3,
                "dtype": "uint8",
                "crs": dst_crs,
                "transform": dst_transform,
                "nodata": nodata,
                "tiled": True,
                "blockxsize": 512,
                "blockysize": 512,
                "compress": "deflate",
                "BIGTIFF": "IF_SAFER",
            }
            with WarpedVRT(src, **vrt_opts) as vrt, rasterio.open(tmp, "w", **profile) as dst:
                src_mask_available = True
                for row_off in range(0, height, block_px):
                    for col_off in range(0, width, block_px):
                        win = Window(
                            col_off,
                            row_off,
                            min(block_px, width - col_off),
                            min(block_px, height - row_off),
                        )
                        data = vrt.read(indexes=[1, 2, 3], window=win)
                        # Mask pixels outside the AOI polygon (in the *output* grid).
                        win_transform = rasterio.windows.transform(win, dst_transform)
                        inside = features.geometry_mask(
                            [aoi_dst],
                            out_shape=(int(win.height), int(win.width)),
                            transform=win_transform,
                            invert=True,
                        )
                        if src_mask_available:
                            try:
                                valid = vrt.read_masks(1, window=win) > 0
                                inside &= valid
                            except Exception:  # pragma: no cover
                                src_mask_available = False
                        # Keep valid pixels away from the nodata value 0 (JPEG sources may hit 0).
                        if nodata == 0:
                            zero = np.all(data == 0, axis=0)
                            data[:, zero & inside] = 1
                        data[:, ~inside] = nodata
                        dst.write(data, window=win)
            cog_translate(
                tmp,
                out_path,
                cog_profiles.get(cog_profile),
                nodata=nodata,
                overview_resampling="average",
                in_memory=False,
                quiet=True,
                config={"GDAL_NUM_THREADS": "ALL_CPUS", "GDAL_TIFF_OVR_BLOCKSIZE": "512"},
            )
    return {
        "path": str(out_path),
        "crs": f"EPSG:{dst_epsg}",
        "gsd_m": target_gsd_m,
        "width": width,
        "height": height,
        "bounds": [minx, maxy - height * target_gsd_m, minx + width * target_gsd_m, maxy],
        "shift_applied_m": [float(dx), float(dy)],
        "resampling": rs.name,
    }
