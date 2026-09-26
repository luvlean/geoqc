"""Fetch the source image into ``data/raw/`` (whole file, or an AOI window through /vsicurl/)."""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import Any

import rasterio
import requests
from rasterio.env import Env
from rasterio.windows import from_bounds
from shapely.geometry.base import BaseGeometry

from geoqc.data.manifest import sha256_of
from geoqc.data.tiling import aoi_to_crs

log = logging.getLogger(__name__)

VSICURL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    "GDAL_HTTP_TIMEOUT": "120",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "5",
}


def remote_size_bytes(url: str, timeout: int = 60) -> int | None:
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        if r.ok and r.headers.get("Content-Length"):
            return int(r.headers["Content-Length"])
    except requests.RequestException as exc:
        log.warning("HEAD %s failed: %s", url, exc)
    return None


def download_file(url: str, dest: Path, *, chunk: int = 1 << 22, retries: int = 5) -> Path:
    """Streaming download with HTTP Range resume into ``dest`` (``dest.part`` while in flight)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    total = remote_size_bytes(url)
    for attempt in range(retries):
        have = part.stat().st_size if part.exists() else 0
        if total is not None and have >= total:
            break
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with requests.get(url, stream=True, timeout=120, headers=headers) as r:
                if have and r.status_code != 206:
                    log.warning("server ignored Range; restarting download")
                    have = 0
                    mode = "wb"
                else:
                    mode = "ab" if have else "wb"
                r.raise_for_status()
                t0 = time.time()
                done = have
                with part.open(mode) as fh:
                    for block in r.iter_content(chunk_size=chunk):
                        fh.write(block)
                        done += len(block)
                log.info(
                    "downloaded %.1f MB in %.0fs (%s)", done / 1e6, time.time() - t0, dest.name
                )
            if total is None or part.stat().st_size >= total:
                break
        except requests.RequestException as exc:
            log.warning("download attempt %d failed: %s", attempt + 1, exc)
            time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"download failed after {retries} attempts: {url}")
    part.replace(dest)
    return dest


def download_window(url: str, dest: Path, aoi_wgs84: BaseGeometry, *, pad_m: float = 50.0) -> Path:
    """Read only the AOI bbox (native CRS/resolution) from a remote COG and save it as GeoTIFF."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Env(**VSICURL_ENV), rasterio.open(f"/vsicurl/{url}") as src:
        aoi_native = aoi_to_crs(aoi_wgs84, src.crs).buffer(pad_m)
        win = from_bounds(*aoi_native.bounds, transform=src.transform)
        win = win.round_offsets().round_lengths()
        win = win.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
        data = src.read(window=win)
        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            width=int(win.width),
            height=int(win.height),
            transform=src.window_transform(win),
            compress="deflate",
            tiled=True,
            blockxsize=512,
            blockysize=512,
        )
        profile.pop("photometric", None)
        with rasterio.open(dest, "w", **profile) as dst:
            dst.write(data)
            with contextlib.suppress(Exception):  # mask is optional
                dst.write_mask(src.read_masks(1, window=win))
    return dest


def image_info(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as ds:
        return {
            "crs": str(ds.crs) if ds.crs else None,
            "gsd_m": float(round((abs(ds.res[0]) + abs(ds.res[1])) / 2.0, 4)),
            "width": ds.width,
            "height": ds.height,
            "count": ds.count,
            "dtype": ds.dtypes[0],
            "bounds": list(ds.bounds),
        }


def fetch_source_image(
    url: str,
    dest: Path,
    *,
    aoi_wgs84: BaseGeometry | None,
    windowed_over_mb: float,
    force: bool = False,
) -> dict[str, Any]:
    """Whole-file download when small enough, otherwise a windowed /vsicurl/ read of the AOI.

    Returns manifest-ready fields (sha256, gsd_m, crs, mode, size_bytes).
    """
    if dest.exists() and not force:
        mode = "existing"
    else:
        size = remote_size_bytes(url)
        if size is not None and size > windowed_over_mb * 1e6 and aoi_wgs84 is not None:
            log.info(
                "remote file is %.0f MB > %.0f MB: windowed read", size / 1e6, windowed_over_mb
            )
            download_window(url, dest, aoi_wgs84)
            mode = "windowed"
        else:
            download_file(url, dest)
            mode = "full"
    info = image_info(dest)
    info.update(
        {
            "sha256": sha256_of(dest),
            "size_bytes": dest.stat().st_size,
            "mode": mode,
            "path": str(dest),
        }
    )
    return info
