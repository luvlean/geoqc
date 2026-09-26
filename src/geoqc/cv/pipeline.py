"""``geoqc cv predict``: tiles -> masks -> merged polygons -> detections.geojson / buildings_det."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
from pyproj import Transformer
from shapely.ops import transform as shp_transform

from geoqc.config import resolve_path
from geoqc.cv.predictor import Predictor, build_predictor
from geoqc.cv.tiles import TileRaster, filter_by_split, load_tile_index, metric_crs_for, read_tile
from geoqc.cv.vectorize import Detection, postprocess, to_geodataframe, vectorize_masks
from geoqc.cv.weights import current_model_version

DET_COLUMNS = ("score", "model_version", "tile_id")


@dataclass
class PredictResult:
    detections: gpd.GeoDataFrame
    model_version: str
    n_tiles: int
    n_tiles_skipped: int
    n_masks: int
    seconds: float
    device: str
    backend: str
    split: str
    raw: dict[str, list[Detection]] = field(default_factory=dict)  # per-tile, pre-merge
    metric_crs: Any = None

    @property
    def n_detections(self) -> int:
        return int(len(self.detections))


def _batches(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i : i + size]


def predict_tiles_raw(
    predictor: Predictor,
    index: gpd.GeoDataFrame,
    *,
    batch: int = 8,
    drop_nodata: bool = True,
    progress: bool = False,
) -> tuple[dict[str, list[Detection]], Any, int, int]:
    """Run the predictor over the indexed tiles; return per-tile Detections in the metric CRS.

    Returns ``(raw_by_tile, metric_crs, n_masks, n_skipped)``; fully-nodata tiles are skipped.
    """
    rows = list(index.itertuples(index=False))
    raw: dict[str, list[Detection]] = {}
    n_masks = 0
    n_skipped = 0
    metric_crs = None
    to_metric: Transformer | None = None
    iterator: Any = _batches(rows, batch)
    if progress:
        from tqdm import tqdm

        iterator = tqdm(list(iterator), desc="predict", unit="batch")
    for chunk in iterator:
        tiles: list[TileRaster] = []
        for row in chunk:
            t = read_tile(row.path, str(row.tile_id))
            if drop_nodata and t.nodata_mask.all():
                n_skipped += 1
                raw[t.tile_id] = []
                continue
            tiles.append(t)
        if not tiles:
            continue
        if metric_crs is None:
            metric_crs = metric_crs_for(tiles[0].crs, index)
            if tiles[0].crs is not None and tiles[0].crs != metric_crs:
                to_metric = Transformer.from_crs(tiles[0].crs, metric_crs, always_xy=True)
        masks_per_tile = predictor.predict_batch([t.image for t in tiles])
        for t, masks in zip(tiles, masks_per_tile, strict=True):
            n_masks += len(masks)
            dets = vectorize_masks(
                masks,
                t.transform,
                t.tile_id,
                valid_mask=(~t.nodata_mask) if drop_nodata else None,
            )
            if to_metric is not None:
                dets = [
                    Detection(shp_transform(to_metric.transform, d.geom), d.score, d.tile_id)
                    for d in dets
                ]
            raw[t.tile_id] = dets
    return raw, metric_crs, n_masks, n_skipped


def run_predict(
    cv_cfg: dict[str, Any],
    pipeline_cfg: dict[str, Any] | None = None,
    *,
    split: str = "all",
    limit: int | None = None,
    device: str | None = None,
    conf: float | None = None,
    backend: str | None = None,
    tiles_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    split_csv: str | Path | None = None,
    predictor: Predictor | None = None,
    model_version: str | None = None,
    progress: bool = False,
    keep_raw: bool = False,
) -> PredictResult:
    """Full detection pipeline for the tiles of ``split`` (``all|val|test``)."""
    t0 = time.perf_counter()
    paths = (pipeline_cfg or {}).get("paths", {}) if pipeline_cfg else {}
    tiles_dir = tiles_dir or paths.get("tiles", "data/tiles")
    index_path = index_path or ((pipeline_cfg or {}).get("tiles", {}) or {}).get("index")
    split_csv = split_csv or Path(paths.get("labels", "data/labels")) / "tiles_split.csv"
    index = load_tile_index(tiles_dir, index_path)
    index = filter_by_split(index, split, split_csv)
    if limit is not None:
        index = index.head(int(limit)).reset_index(drop=True)
    if predictor is None:
        predictor = build_predictor(cv_cfg, device=device, conf=conf, backend=backend)
    backend_name = backend or str(cv_cfg.get("backend", "ultralytics"))
    mv = model_version or (
        current_model_version(cv_cfg) if predictor.name != "mock" else "mock-00000000"
    )
    vec_cfg = dict(cv_cfg.get("vectorize") or {})
    raw, metric_crs, n_masks, n_skipped = predict_tiles_raw(
        predictor,
        index,
        batch=int(cv_cfg.get("batch", 8)),
        drop_nodata=bool(vec_cfg.get("drop_nodata", True)),
        progress=progress,
    )
    all_dets = [d for dets in raw.values() for d in dets]
    final = postprocess(all_dets, vec_cfg)
    if metric_crs is None:  # no readable tiles
        metric_crs = "EPSG:4326"
    gdf = to_geodataframe(final, metric_crs, mv)
    return PredictResult(
        detections=gdf,
        model_version=mv,
        n_tiles=int(len(index)),
        n_tiles_skipped=n_skipped,
        n_masks=n_masks,
        seconds=round(time.perf_counter() - t0, 3),
        device=getattr(predictor, "device", "cpu"),
        backend=backend_name,
        split=split,
        raw=raw if keep_raw else {},
        metric_crs=metric_crs,
    )


def default_detections_path(split: str, interim_dir: str | Path = "data/interim") -> Path:
    name = "detections.geojson" if split == "all" else f"detections_{split}.geojson"
    return Path(resolve_path(interim_dir)) / name


def write_detections(gdf: gpd.GeoDataFrame, out_path: str | Path) -> Path:
    """Write a contract §3.6 FeatureCollection (EPSG:4326, MultiPolygon)."""
    out = Path(resolve_path(out_path))
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(gdf) == 0:
        out.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
        return out
    gdf = gdf[[*DET_COLUMNS, "geometry"]]
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.set_crs("EPSG:4326") if gdf.crs is None else gdf.to_crs("EPSG:4326")
    gdf.to_file(out, driver="GeoJSON", layer="detections")
    return out


def read_detections(path: str | Path) -> gpd.GeoDataFrame:
    p = Path(resolve_path(path))
    text = p.read_text(encoding="utf-8")
    if not json.loads(text).get("features"):
        return gpd.GeoDataFrame(
            {c: [] for c in DET_COLUMNS}, geometry=gpd.GeoSeries([], crs="EPSG:4326")
        )
    gdf = gpd.read_file(p)
    return gdf.set_crs("EPSG:4326") if gdf.crs is None else gdf


def load_to_db(
    gdf: gpd.GeoDataFrame,
    model_version: str,
    conn: Any | None = None,
    *,
    replace: bool = True,
    dsn: str | None = None,
) -> int:
    """Insert detections into ``buildings_det``; with ``replace`` old rows of the same
    model_version are deleted first (cascades to matches/discrepancies of that version)."""
    from geoqc.db import connect

    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    rows = [
        (geom.wkt, float(score), model_version, None if tile_id is None else str(tile_id))
        for geom, score, tile_id in zip(gdf.geometry, gdf["score"], gdf["tile_id"], strict=True)
        if geom is not None and not geom.is_empty
    ]

    def _run(c: Any) -> int:
        with c.cursor() as cur:
            if replace:
                cur.execute("DELETE FROM buildings_det WHERE model_version = %s", (model_version,))
            if rows:
                cur.executemany(
                    "INSERT INTO buildings_det (geom, score, model_version, tile_id) "
                    "VALUES (ST_Multi(ST_GeomFromText(%s, 4326)), %s, %s, %s)",
                    rows,
                )
        return len(rows)

    if conn is not None:
        return _run(conn)
    with connect(dsn) as c:
        return _run(c)


def summarize(result: PredictResult) -> dict[str, Any]:
    return {
        "split": result.split,
        "backend": result.backend,
        "device": result.device,
        "model_version": result.model_version,
        "n_tiles": result.n_tiles,
        "n_tiles_skipped_nodata": result.n_tiles_skipped,
        "n_masks_raw": result.n_masks,
        "n_detections": result.n_detections,
        "seconds": result.seconds,
        "score_mean": float(np.mean(result.detections["score"])) if result.n_detections else None,
    }
