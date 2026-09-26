"""Imagery basemap: ``GET /imagery/{z}/{x}/{y}.png`` rendered from ``data/interim/aoi_cog.tif`` (rio-tiler)."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Response
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.models import ImageData

from geoqc.api.deps import AppContext, get_ctx
from geoqc.api.routes.tiles import validate_tile

log = logging.getLogger("geoqc.api.imagery")
router = APIRouter(tags=["imagery"])

PNG_MEDIA_TYPE = "image/png"


@lru_cache(maxsize=4)
def transparent_png(size: int) -> bytes:
    """Fully transparent RGBA PNG of ``size``x``size`` px (returned outside the COG footprint)."""
    data = np.ma.MaskedArray(
        np.zeros((3, size, size), dtype="uint8"), mask=np.ones((3, size, size), dtype=bool)
    )
    return ImageData(data).render(img_format="PNG", add_mask=True)


@router.get("/imagery/{z}/{x}/{y}.png", summary="Imagery tile from the AOI COG")
def get_imagery_tile(
    z: int, x: int, y: int, ctx: Annotated[AppContext, Depends(get_ctx)]
) -> Response:
    """PNG tile in WebMercatorQuad; transparent outside coverage; 404 when the COG is absent."""
    validate_tile(z, x, y, int(ctx.config["mvt"]["max_zoom"]))
    cog = ctx.paths["cog"]
    if not cog.exists():
        raise HTTPException(status_code=404, detail=f"imagery not available: {cog} not found")
    img_cfg = ctx.config["imagery"]
    tile_size = int(img_cfg.get("tile_size", 256))
    headers = {"Cache-Control": ctx.cache_header("imagery")}
    try:
        with Reader(str(cog)) as src:
            if not src.tile_exists(x, y, z):
                return Response(
                    transparent_png(tile_size), media_type=PNG_MEDIA_TYPE, headers=headers
                )
            img = src.tile(
                x, y, z, tilesize=tile_size, resampling_method=img_cfg.get("resampling", "bilinear")
            )
    except TileOutsideBounds:
        return Response(transparent_png(tile_size), media_type=PNG_MEDIA_TYPE, headers=headers)
    except Exception as exc:  # rasterio/GDAL errors -> 500 with a readable message
        log.exception("imagery tile %s/%s/%s failed", z, x, y)
        raise HTTPException(status_code=500, detail=f"imagery rendering failed: {exc}") from exc
    return Response(
        img.render(img_format="PNG", add_mask=True), media_type=PNG_MEDIA_TYPE, headers=headers
    )
