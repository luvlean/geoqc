"""External training set: SpaceNet 2 Buildings (CC BY-SA 4.0) -> YOLO-seg dataset.

``fetch_spacenet`` downloads a deterministic sample of labelled tiles (``PS-RGB`` GeoTIFF +
``geojson_buildings``) from the public ``spacenet-dataset`` S3 bucket over HTTPS — no AWS account.
``prepare_spacenet`` brings every tile to the geometry of our own tiles: reprojection to the local
UTM zone at ``gsd_m`` (0.3 m, square pixels), a ``size_px`` chip centred on the source tile,
11-bit -> 8-bit percentile stretch; polygons are clipped to the chip and dropped below
``min_area_m2`` (same cut as ``annotation.min_area_m2``).

Output layout ``data/interim/external/spacenet2/`` matches ``geoqc.cv.dataset``::

    images/{train,holdout}/<chip_id>.png   labels/{train,holdout}/<chip_id>.txt
    masks/{train,holdout}/<chip_id>.png    data.yaml  split.csv  meta.json  buildings.gpkg

Train/holdout is split per chip, stratified by city. None of this touches ``data/labels``.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
import yaml
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds
from shapely.geometry import box

from geoqc.config import repo_relative, resolve_path
from geoqc.cv.dataset import SUBSETS, polygon_to_yolo_lines, rasterize_mask, split_tiles
from geoqc.cv.tiles import utm_epsg_for
from geoqc.cv.vectorize import polygons_only

LICENSE = "CC BY-SA 4.0"
ATTRIBUTION = "SpaceNet on AWS (SpaceNet Partners, Maxar WorldView-3), CC BY-SA 4.0"
HOMEPAGE = "https://spacenet.ai/spacenet-buildings-dataset-v2/"
DEFAULTS: dict[str, Any] = {
    "base_url": "https://spacenet-dataset.s3.amazonaws.com",
    "prefix": "spacenet/SN2_buildings/train",
    "cities": ["AOI_3_Paris", "AOI_4_Shanghai", "AOI_2_Vegas"],
    "per_city": 100,
    "seed": 42,
    "raw_dir": "data/raw/external/spacenet2",
    "out_dir": "data/interim/external/spacenet2",
    "gsd_m": 0.3,
    "size_px": 512,
    "stretch_percentiles": [2.0, 98.0],
    "holdout_fraction": 0.2,
    "min_area_m2": 12.0,
    "min_valid_fraction": 0.5,
    "workers": 8,
}
_IMG_RE = re.compile(r"_img(\d+)\.tif$")
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def external_config(cv_cfg: dict[str, Any] | None) -> dict[str, Any]:
    """``cv.external`` merged over ``DEFAULTS``."""
    return {**DEFAULTS, **((cv_cfg or {}).get("external") or {})}


# ---------------------------------------------------------------- download


def parse_s3_listing(xml_text: str) -> tuple[list[tuple[str, int]], str | None]:
    """ListObjectsV2 XML -> ([(key, size)], next continuation token or None)."""
    root = ET.fromstring(xml_text)
    items = [
        (c.findtext(f"{_S3_NS}Key") or "", int(c.findtext(f"{_S3_NS}Size") or 0))
        for c in root.iter(f"{_S3_NS}Contents")
    ]
    truncated = (root.findtext(f"{_S3_NS}IsTruncated") or "false").lower() == "true"
    token = root.findtext(f"{_S3_NS}NextContinuationToken") if truncated else None
    return items, token


def list_keys(
    base_url: str, prefix: str, session: requests.Session | None = None
) -> list[tuple[str, int]]:
    """All object keys under ``prefix`` of a public bucket (paginated ListObjectsV2)."""
    http = session or requests.Session()
    out: list[tuple[str, int]] = []
    token: str | None = None
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        r = http.get(base_url.rstrip("/") + "/", params=params, timeout=60)
        r.raise_for_status()
        items, token = parse_s3_listing(r.text)
        out += items
        if not token:
            return out


def image_ids(keys: Iterable[str]) -> list[int]:
    """Numeric ``imgNNN`` ids of the PS-RGB tiles among ``keys``, sorted."""
    return sorted({int(m.group(1)) for k in keys if (m := _IMG_RE.search(k))})


def sample_ids(ids: list[int], n: int, seed: int) -> list[int]:
    """Deterministic sample of ``n`` ids (all of them when ``n`` is 0 or too large)."""
    ids = sorted(ids)
    if n <= 0 or n >= len(ids):
        return ids
    return sorted(random.Random(seed).sample(ids, n))


def tile_keys(prefix: str, city: str, img_id: int) -> dict[str, str]:
    stem = f"SN2_buildings_train_{city}"
    return {
        "image": f"{prefix}/{city}/PS-RGB/{stem}_PS-RGB_img{img_id}.tif",
        "labels": f"{prefix}/{city}/geojson_buildings/{stem}_geojson_buildings_img{img_id}.geojson",
    }


def _download(url: str, dest: Path, session: requests.Session, retries: int = 3) -> bool:
    """Stream ``url`` to ``dest`` (atomic rename); False when the file already existed."""
    if dest.exists() and dest.stat().st_size > 0:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(retries):
        try:
            with session.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            tmp.replace(dest)
            return True
        except requests.RequestException:
            if attempt == retries - 1:
                tmp.unlink(missing_ok=True)
                raise
    return False


def fetch_spacenet(
    cv_cfg: dict[str, Any] | None = None,
    *,
    cities: list[str] | None = None,
    per_city: int | None = None,
    raw_dir: str | Path | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Download ``per_city`` labelled tiles per city into ``raw_dir``; writes ``SOURCE.json``."""
    cfg = external_config(cv_cfg)
    cities = list(cities or cfg["cities"])
    n = int(per_city if per_city is not None else cfg["per_city"])
    raw = Path(resolve_path(raw_dir or cfg["raw_dir"]))
    http = session or requests.Session()
    base, prefix = cfg["base_url"].rstrip("/"), cfg["prefix"].strip("/")
    summary: dict[str, Any] = {"cities": {}, "n_downloaded": 0, "n_cached": 0}
    for city in cities:
        keys = [k for k, _ in list_keys(base, f"{prefix}/{city}/PS-RGB/", http)]
        ids = sample_ids(image_ids(keys), n, int(cfg["seed"]))
        jobs = []
        for i in ids:
            for kind, key in tile_keys(prefix, city, i).items():
                sub = "PS-RGB" if kind == "image" else "geojson_buildings"
                jobs.append((f"{base}/{key}", raw / city / sub / Path(key).name))
        with ThreadPoolExecutor(max_workers=int(cfg["workers"])) as pool:
            fresh = list(pool.map(lambda j: _download(j[0], j[1], http), jobs))
        summary["cities"][city] = {"n_available": len(image_ids(keys)), "n_selected": len(ids)}
        summary["n_downloaded"] += sum(fresh)
        summary["n_cached"] += len(fresh) - sum(fresh)
    source = {
        "dataset": "SpaceNet 2 Buildings (train)",
        "homepage": HOMEPAGE,
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "bucket_url": f"{base}/{prefix}",
        "seed": int(cfg["seed"]),
        "per_city": n,
        "cities": summary["cities"],
        "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "SOURCE.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
    summary["raw_dir"] = str(raw)
    return repo_relative(summary)


# ---------------------------------------------------------------- prepare


@dataclass
class Chip:
    chip_id: str
    city: str
    image: np.ndarray  # size x size x 3 uint8 RGB
    valid: np.ndarray  # size x size bool: pixel covered by the source tile
    transform: Any
    crs: str
    buildings: gpd.GeoDataFrame  # clipped polygons in ``crs`` with ``area_m2``, ``partial``


def stretch_to_uint8(
    bands: np.ndarray, valid: np.ndarray, percentiles: tuple[float, float] = (2.0, 98.0)
) -> np.ndarray:
    """Per-band percentile stretch of a (3, H, W) array over valid pixels -> H x W x 3 uint8."""
    out = np.zeros((*bands.shape[1:], bands.shape[0]), dtype=np.uint8)
    for b in range(bands.shape[0]):
        vals = bands[b][valid]
        if vals.size == 0:
            continue
        lo, hi = np.percentile(vals, percentiles)
        scaled = (bands[b].astype(np.float32) - lo) / max(float(hi - lo), 1e-6)
        out[..., b] = np.clip(scaled * 255.0 + 0.5, 0, 255).astype(np.uint8)
    out[~valid] = 0
    return out


def prepare_chip(
    image_path: Path,
    labels_path: Path | None,
    city: str,
    *,
    gsd_m: float = 0.3,
    size_px: int = 512,
    percentiles: tuple[float, float] = (2.0, 98.0),
    min_area_m2: float = 12.0,
    min_valid_in_polygon: float = 0.5,
) -> Chip:
    """One SpaceNet tile -> ``size_px`` chip at ``gsd_m`` in local UTM with clipped polygons."""
    m = _IMG_RE.search(image_path.name)
    chip_id = f"{city}_img{m.group(1)}" if m else f"{city}_{image_path.stem}"
    with rasterio.open(image_path) as src:
        west, south, east, north = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        epsg = utm_epsg_for((west + east) / 2, (south + north) / 2)
        x0, y0, x1, y1 = transform_bounds(src.crs, f"EPSG:{epsg}", *src.bounds)
        half = size_px * gsd_m / 2
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        dst_transform = from_origin(mx - half, my + half, gsd_m, gsd_m)
        src_bands = src.read(out_dtype="float32")
        dst = np.zeros((src.count, size_px, size_px), dtype=np.float32)
        reproject(
            src_bands,
            dst,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=f"EPSG:{epsg}",
            resampling=Resampling.bilinear,
            src_nodata=0,
            dst_nodata=0,
        )
    valid = np.any(dst > 0, axis=0)
    image = stretch_to_uint8(dst[:3], valid, percentiles)

    crs = f"EPSG:{epsg}"
    frame = box(mx - half, my - half, mx + half, my + half)
    empty = gpd.GeoDataFrame({"area_m2": [], "partial": []}, geometry=[], crs=crs)
    buildings = empty
    if labels_path is not None and labels_path.exists() and labels_path.stat().st_size > 0:
        raw = gpd.read_file(labels_path)
        if len(raw):
            raw = raw.set_crs("EPSG:4326", allow_override=True).to_crs(crs)
            geoms, partial = [], []
            for g in raw.geometry:
                if g is None or g.is_empty:
                    continue
                clipped = polygons_only(g.buffer(0).intersection(frame))
                if clipped.is_empty or clipped.area < min_area_m2:
                    continue
                # outlines drawn over nodata teach the model to find buildings in black pixels
                inside = geometry_mask([clipped], valid.shape, dst_transform, invert=True)
                if not inside.any() or valid[inside].mean() < min_valid_in_polygon:
                    continue
                geoms.append(clipped)
                partial.append(bool(clipped.area < g.area * 0.999))
            if geoms:
                buildings = gpd.GeoDataFrame(
                    {"area_m2": [g.area for g in geoms], "partial": partial},
                    geometry=geoms,
                    crs=crs,
                )
    return Chip(chip_id, city, image, valid, dst_transform, crs, buildings)


def _raw_pairs(raw: Path, cities: list[str]) -> list[tuple[str, Path, Path]]:
    pairs = []
    for city in cities:
        for img in sorted((raw / city / "PS-RGB").glob("*.tif")):
            m = _IMG_RE.search(img.name)
            if not m:
                continue
            lab = (
                raw
                / city
                / "geojson_buildings"
                / f"SN2_buildings_train_{city}_geojson_buildings_img{m.group(1)}.geojson"
            )
            pairs.append((city, img, lab))
    return pairs


def prepare_spacenet(
    cv_cfg: dict[str, Any] | None = None,
    *,
    raw_dir: str | Path | None = None,
    out_dir: str | Path | None = None,
    cities: list[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Raw SpaceNet tiles -> YOLO-seg dataset + masks + ``buildings.gpkg`` (EPSG:4326)."""
    cfg = external_config(cv_cfg)
    raw = Path(resolve_path(raw_dir or cfg["raw_dir"]))
    out = Path(resolve_path(out_dir or cfg["out_dir"]))
    cities = list(cities or cfg["cities"])
    pairs = _raw_pairs(raw, cities)
    if not pairs:
        raise FileNotFoundError(f"no SpaceNet tiles under {raw}; run `geoqc cv fetch-external`")
    chip_ids = [f"{c}_img{_IMG_RE.search(p.name).group(1)}" for c, p, _ in pairs]
    assignment = split_tiles(
        chip_ids,
        holdout_fraction=float(cfg["holdout_fraction"]),
        seed=int(cfg["seed"]),
        strata={cid: c for cid, (c, _, _) in zip(chip_ids, pairs, strict=True)},
    )
    for kind in ("images", "labels", "masks"):
        shutil.rmtree(out / kind, ignore_errors=True)  # no stale chips from a previous run
        for sub in SUBSETS:
            (out / kind / sub).mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    n_skipped = 0
    frames: list[gpd.GeoDataFrame] = []
    size = int(cfg["size_px"])
    for i, (city, img, lab) in enumerate(pairs):
        chip = prepare_chip(
            img,
            lab,
            city,
            gsd_m=float(cfg["gsd_m"]),
            size_px=size,
            percentiles=tuple(cfg["stretch_percentiles"]),
            min_area_m2=float(cfg["min_area_m2"]),
        )
        if chip.valid.mean() < float(cfg["min_valid_fraction"]):
            n_skipped += 1
            continue
        sub = assignment[chip.chip_id]
        cv2.imwrite(str(out / "images" / sub / f"{chip.chip_id}.png"), chip.image[:, :, ::-1])
        lines: list[str] = []
        for g in chip.buildings.geometry:
            lines.extend(polygon_to_yolo_lines(g, chip.transform, size, size))
        (out / "labels" / sub / f"{chip.chip_id}.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
        mask = rasterize_mask(list(chip.buildings.geometry), chip.transform, (size, size))
        cv2.imwrite(str(out / "masks" / sub / f"{chip.chip_id}.png"), mask)
        valid_frac = float(chip.valid.mean())
        rows.append(
            {
                "tile_id": chip.chip_id,
                "subset": sub,
                "city": city,
                "n_instances": len(chip.buildings),
                "n_partial": int(chip.buildings["partial"].sum()) if len(chip.buildings) else 0,
                "coverage": float((mask > 0).sum() / max(chip.valid.sum(), 1)),
                "valid_fraction": valid_frac,
                "crs": chip.crs,
                "source": img.name,
            }
        )
        if len(chip.buildings):
            b = chip.buildings.copy()
            b["chip_id"], b["city"], b["subset"] = chip.chip_id, city, sub
            b["n_vertices"] = [len(g.exterior.coords) - 1 for g in _largest_parts(b.geometry)]
            b["n_holes"] = [len(g.interiors) for g in _largest_parts(b.geometry)]
            frames.append(b.to_crs("EPSG:4326"))
        if progress:
            progress(i + 1, len(pairs))

    split = pd.DataFrame(rows).sort_values("tile_id")
    split.to_csv(out / "split.csv", index=False)
    if frames:
        gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs="EPSG:4326").to_file(
            out / "buildings.gpkg", driver="GPKG"
        )
    data_yaml = {
        "path": str(out),
        "train": "images/train",
        "val": "images/holdout",
        "names": {0: "building"},
        "nc": 1,
    }
    (out / "data.yaml").write_text(yaml.safe_dump(data_yaml, sort_keys=False), encoding="utf-8")
    meta = {
        "dataset": "SpaceNet 2 Buildings (train subset)",
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "gsd_m": float(cfg["gsd_m"]),
        "size_px": size,
        "min_area_m2": float(cfg["min_area_m2"]),
        "stretch_percentiles": list(cfg["stretch_percentiles"]),
        "seed": int(cfg["seed"]),
        "holdout_fraction": float(cfg["holdout_fraction"]),
        "n_train": int((split["subset"] == "train").sum()),
        "n_holdout": int((split["subset"] == "holdout").sum()),
        "n_instances": int(split["n_instances"].sum()),
        "n_skipped_nodata": n_skipped,
        "by_city": {
            c: {"chips": int(len(g)), "instances": int(g["n_instances"].sum())}
            for c, g in split.groupby("city")
        },
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return repo_relative({"out_dir": str(out), **meta})


def _largest_parts(geoms: Iterable[Any]) -> list[Any]:
    return [max(g.geoms, key=lambda p: p.area) if hasattr(g, "geoms") else g for g in geoms]
