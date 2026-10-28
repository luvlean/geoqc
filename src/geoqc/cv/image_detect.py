"""Building detection on an arbitrary image (web upload): GeoTIFF, PNG, JPEG, ... read via GDAL.

The model was trained at 0.3 m/px, so the image is resampled to ``target_gsd_m`` first. The pixel
size comes from the georeference (projected CRS, or degrees converted at the image centre) or from
the caller (``gsd_m``) for plain pictures. The resampled image is cut into tiles with overlap like
the AOI (``cv.tiles``), every tile goes through the predictor, and :func:`vectorize.postprocess`
merges the parts of buildings cut by tile edges. Output geometries are in pixels of the resampled
image (for the web viewer) and, for georeferenced input, in EPSG:4326.
"""

from __future__ import annotations

import base64
import io
import math
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import transform_geom
from shapely.affinity import affine_transform, scale
from shapely.geometry import mapping

from geoqc.cv.predictor import Mask, Predictor
from geoqc.cv.vectorize import Detection, as_multipolygon, postprocess, vectorize_masks

TILE, OVERLAP = 512, 64


class ImageError(ValueError):
    """The upload cannot be processed (format, size, missing pixel size); message is user-facing."""


@dataclass
class LoadedImage:
    rgb: np.ndarray  # H x W x 3 uint8 at target_gsd_m
    valid: np.ndarray  # H x W bool, False on nodata / transparent pixels
    gsd_m: float  # pixel size of rgb
    source_gsd_m: float
    source_size: tuple[int, int]  # (width, height) of the original
    gsd_from: str  # "georeference" | "user" | "default"
    crs: Any = None  # rasterio CRS of the original when georeferenced
    transform: Affine | None = None  # pixel of rgb -> CRS coordinates


@dataclass
class ImageDetections:
    image: LoadedImage
    detections: list[Detection]  # geometry in pixels of image.rgb
    seconds: float
    n_tiles: int
    geo: list[dict[str, Any]] = field(default_factory=list)  # GeoJSON geometries, EPSG:4326


def _pixel_size_m(src: rasterio.io.DatasetReader) -> float | None:
    if src.crs is None or src.transform.is_identity:
        return None
    px = (abs(src.transform.a) + abs(src.transform.e)) / 2
    if src.crs.is_projected:
        return px * float(src.crs.linear_units_factor[1])
    if src.crs.is_geographic:
        lat = src.xy(src.height // 2, src.width // 2)[1]
        return px * 111_320.0 * math.cos(math.radians(lat))
    return None


def _to_uint8(arr: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Bands as uint8; 16-bit / float imagery is stretched by the 2–98 % percentiles of valid pixels."""
    if arr.dtype == np.uint8:
        return arr
    out = np.zeros(arr.shape, np.uint8)
    for b in range(arr.shape[0]):
        vals = arr[b][valid]
        lo, hi = np.percentile(vals, (2, 98)) if vals.size else (0.0, 1.0)
        out[b] = np.clip((arr[b] - lo) / max(hi - lo, 1e-9) * 255, 0, 255).astype(np.uint8)
    return out


def load_image(
    path: str | Path,
    *,
    gsd_m: float | None = None,
    default_gsd_m: float = 0.3,
    target_gsd_m: float = 0.3,
    max_side_px: int = 6144,
) -> LoadedImage:
    """Read ``path`` and resample it to ``target_gsd_m``; ``gsd_m`` overrides the georeference."""
    try:
        with warnings.catch_warnings():  # a plain PNG/JPEG has no georeference by design
            warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
            src = rasterio.open(path)
    except rasterio.errors.RasterioIOError as exc:
        raise ImageError(
            "формат не распознан: нужен GeoTIFF, PNG, JPEG или другой растр GDAL"
        ) from exc
    with src:
        geo_gsd = _pixel_size_m(src)
        if gsd_m:
            source_gsd, gsd_from = float(gsd_m), "user"
        elif geo_gsd:
            source_gsd, gsd_from = geo_gsd, "georeference"
        else:
            source_gsd, gsd_from = float(default_gsd_m), "default"
        factor = source_gsd / target_gsd_m
        w, h = max(1, round(src.width * factor)), max(1, round(src.height * factor))
        if max(w, h) > max_side_px:
            raise ImageError(
                f"после приведения к {target_gsd_m} м/пикс снимок {w}×{h} px больше лимита "
                f"{max_side_px} px: обрежьте его или проверьте размер пикселя"
            )
        bands = [1, 2, 3] if src.count >= 3 else [1, 1, 1]
        rs = Resampling.average if factor < 1 else Resampling.bilinear
        arr = src.read(bands, out_shape=(3, h, w), resampling=rs)
        valid = src.read_masks(1, out_shape=(h, w), resampling=Resampling.nearest) > 0
        georef = geo_gsd is not None
        transform = src.transform @ Affine.scale(src.width / w, src.height / h) if georef else None
        crs = src.crs if georef else None
        size = (src.width, src.height)
    rgb = np.transpose(_to_uint8(arr, valid), (1, 2, 0))
    return LoadedImage(
        rgb=np.ascontiguousarray(rgb),
        valid=valid,
        gsd_m=target_gsd_m,
        source_gsd_m=source_gsd,
        source_size=size,
        gsd_from=gsd_from,
        crs=crs,
        transform=transform,
    )


def _starts(n: int) -> list[int]:
    if n <= TILE:
        return [0]
    step = TILE - OVERLAP
    out = list(range(0, n - TILE + 1, step))
    if out[-1] != n - TILE:
        out.append(n - TILE)
    return out


def detect(
    img: LoadedImage,
    predictor: Predictor,
    vec_cfg: dict[str, Any],
    *,
    conf: float = 0.5,
    batch: int = 8,
) -> ImageDetections:
    """Tile, predict, vectorize and merge; detections below ``conf`` are dropped before merging."""
    t0 = time.perf_counter()
    h, w = img.valid.shape
    cells = [(r, c) for r in _starts(h) for c in _starts(w)]
    tiles, valids = [], []
    for r, c in cells:
        tile = np.zeros((TILE, TILE, 3), np.uint8)
        valid = np.zeros((TILE, TILE), bool)
        part = img.rgb[r : r + TILE, c : c + TILE]
        tile[: part.shape[0], : part.shape[1]] = part
        valid[: part.shape[0], : part.shape[1]] = img.valid[r : r + TILE, c : c + TILE]
        tiles.append(tile)
        valids.append(valid)
    raw: list[Detection] = []
    gsd = img.gsd_m
    for k in range(0, len(cells), batch):
        masks_per_tile: list[list[Mask]] = predictor.predict_batch(tiles[k : k + batch])
        for (r, c), masks, valid in zip(
            cells[k : k + batch], masks_per_tile, valids[k : k + batch], strict=True
        ):
            if not valid.any():
                continue
            kept = [m for m in masks if m.score >= conf]
            # metres in a local frame with y down: areas and the 0.5 m simplification stay metric
            tr = Affine(gsd, 0, c * gsd, 0, gsd, r * gsd)
            raw += vectorize_masks(kept, tr, f"{r}_{c}", valid_mask=valid)
    merged = postprocess(raw, vec_cfg)
    dets = [
        Detection(
            geom=scale(d.geom, 1 / gsd, 1 / gsd, origin=(0, 0)), score=d.score, tile_id=d.tile_id
        )
        for d in merged
    ]
    geo = []
    if img.transform is not None and img.crs is not None:
        t = img.transform
        for d in dets:
            g = affine_transform(as_multipolygon(d.geom), [t.a, t.b, t.d, t.e, t.c, t.f])
            geo.append(transform_geom(img.crs, "EPSG:4326", mapping(g)))
    return ImageDetections(
        image=img,
        detections=dets,
        seconds=time.perf_counter() - t0,
        n_tiles=len(cells),
        geo=geo,
    )


def preview_data_url(img: LoadedImage, max_side_px: int = 3072, quality: int = 85) -> str:
    """JPEG preview of the resampled image (the viewer stretches it to the full pixel extent)."""
    pil = Image.fromarray(img.rgb)
    side = max(pil.size)
    if side > max_side_px:
        k = max_side_px / side
        pil = pil.resize(
            (max(1, round(pil.width * k)), max(1, round(pil.height * k))), Image.BILINEAR
        )
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def to_feature_collections(res: ImageDetections) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(pixel FeatureCollection, EPSG:4326 FeatureCollection or None); area in m², id from 1."""
    px_area = res.image.gsd_m**2
    pixel, geo = [], []
    order = sorted(range(len(res.detections)), key=lambda k: -res.detections[k].score)
    for i, k in enumerate(order, start=1):
        d = res.detections[k]
        props = {"id": i, "score": round(d.score, 4), "area_m2": round(d.geom.area * px_area, 1)}
        pixel.append(
            {"type": "Feature", "properties": props, "geometry": mapping(as_multipolygon(d.geom))}
        )
        if res.geo:
            geo.append({"type": "Feature", "properties": props, "geometry": res.geo[k]})
    geo_fc = {"type": "FeatureCollection", "features": geo} if res.geo else None
    return {"type": "FeatureCollection", "features": pixel}, geo_fc
