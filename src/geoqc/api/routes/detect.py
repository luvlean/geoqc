"""Building detection on an uploaded image: ``GET /detect/config`` and ``POST /detect``.

The model (``config/cv.yaml``) is loaded on the first request and reused; inference runs under a
lock (one GPU/MPS). Pixel size, resampling and tiling: :mod:`geoqc.cv.image_detect`. Nothing is
written to the database; the upload is deleted after the request.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from geoqc.api.deps import AppContext, get_ctx

log = logging.getLogger("geoqc.api.detect")
router = APIRouter(tags=["detect"])
_LOCK = threading.Lock()


def _cv_config() -> dict[str, Any]:
    from geoqc.config import load_config

    return load_config()["cv"]


def _detector(ctx: AppContext) -> Any:
    """Predictor built once per app; tests put a MockPredictor into ``ctx.detector``."""
    if ctx.detector is None:
        from geoqc.cv.predictor import build_predictor

        try:
            ctx.detector = build_predictor(
                _cv_config(), conf=float(ctx.config["detect"]["min_conf"])
            )
        except Exception as exc:  # missing weights / broken cv config must not take the API down
            log.warning("cannot load the detection model: %s", exc)
            raise HTTPException(status_code=503, detail=f"модель не загружена: {exc}") from exc
    return ctx.detector


@router.get("/detect/config", summary="Limits and defaults of the upload detection")
def detect_config(ctx: Annotated[AppContext, Depends(get_ctx)]) -> dict[str, Any]:
    cv = _cv_config()
    d = ctx.config["detect"]
    return {
        "model_version": cv.get("model_version"),
        "conf_default": float(cv.get("conf_threshold", 0.5)),
        "min_conf": float(d["min_conf"]),
        "target_gsd_m": float(d["target_gsd_m"]),
        "default_gsd_m": float(d["default_gsd_m"]),
        "max_upload_mb": int(d["max_upload_mb"]),
        "max_side_px": int(d["max_side_px"]),
    }


@router.post("/detect", summary="Detect buildings on an uploaded image")
def detect(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    file: Annotated[UploadFile, File(description="GeoTIFF, PNG, JPEG or another GDAL raster")],
    gsd_m: Annotated[
        float | None, Form(gt=0, le=10, description="pixel size, m (overrides the georeference)")
    ] = None,
    conf: Annotated[float | None, Form(ge=0, le=1, description="confidence threshold")] = None,
) -> dict[str, Any]:
    """Resample to 0.3 m/px, tile, run the model, merge outlines across tiles.

    Returns a JPEG preview, outlines in preview pixels and, for georeferenced input, in EPSG:4326.
    """
    from geoqc.cv import image_detect

    d = ctx.config["detect"]
    cv = _cv_config()
    conf = max(
        float(conf if conf is not None else cv.get("conf_threshold", 0.5)), float(d["min_conf"])
    )
    limit = int(d["max_upload_mb"]) * 1024 * 1024
    suffix = Path(file.filename or "").suffix.lower()[:10]
    with tempfile.TemporaryDirectory(prefix="geoqc-detect-") as tmp:
        path = Path(tmp) / f"upload{suffix}"
        with path.open("wb") as out:
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413, detail=f"файл больше {d['max_upload_mb']} МБ"
                    )
                out.write(chunk)
        if path.stat().st_size == 0:
            raise HTTPException(status_code=422, detail="пустой файл")
        try:
            img = image_detect.load_image(
                path,
                gsd_m=gsd_m,
                default_gsd_m=float(d["default_gsd_m"]),
                target_gsd_m=float(d["target_gsd_m"]),
                max_side_px=int(d["max_side_px"]),
            )
        except image_detect.ImageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    with _LOCK:  # one model load and one inference at a time (single GPU/MPS)
        res = image_detect.detect(img, _detector(ctx), cv.get("vectorize") or {}, conf=conf)
    pixel, wgs84 = image_detect.to_feature_collections(res)
    h, w = img.valid.shape
    log.info(
        "detect %s: %dx%d px, %d tiles, %d buildings, %.1f s",
        file.filename,
        w,
        h,
        res.n_tiles,
        len(res.detections),
        res.seconds,
    )
    return {
        "filename": file.filename,
        "model_version": cv.get("model_version"),
        "conf": conf,
        "width": w,
        "height": h,
        "gsd_m": img.gsd_m,
        "source_gsd_m": round(img.source_gsd_m, 4),
        "gsd_from": img.gsd_from,
        "source_size": list(img.source_size),
        "georeferenced": wgs84 is not None,
        "n_tiles": res.n_tiles,
        "seconds": round(res.seconds, 2),
        "image": image_detect.preview_data_url(img),
        "features": pixel,
        "features_wgs84": wgs84,
    }
