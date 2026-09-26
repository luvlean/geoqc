"""Mask -> polygons -> merged, simplified, filtered MultiPolygons in EPSG:4326.

Pipeline (all geometry work in the metric tile CRS, see README for formulas):

1. ``mask_to_geometry`` - ``rasterio.features.shapes`` on the boolean mask, pixel -> CRS via
   the tile affine transform, ``make_valid``.
2. ``merge_overlaps`` - union-find over pairs of detections from *different* tiles whose
   ``IoU >= tile_overlap_merge_iou`` **or** ``intersection / min(area) >= min_overlap_ratio``;
   each group -> ``unary_union``; score = ``max`` or area-weighted mean.
3. ``simplify_detections`` - Douglas-Peucker with ``tolerance`` in metres (topology preserved).
4. ``filter_min_area`` - drop polygons with ``area < min_area_m2``.
5. ``to_geodataframe`` - MultiPolygon, ``make_valid``, reproject to EPSG:4326.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import geopandas as gpd
import numpy as np
from rasterio import features
from rasterio.transform import Affine
from shapely import STRtree, make_valid, unary_union
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry

from geoqc.cv.predictor import Mask


@dataclass
class Detection:
    """One detected building in the metric CRS of the tiles."""

    geom: BaseGeometry
    score: float
    tile_id: str

    @property
    def area(self) -> float:
        return float(self.geom.area)


def polygons_only(geom: BaseGeometry) -> BaseGeometry:
    """Keep only polygonal parts of a geometry (drops lines/points from make_valid output)."""
    if geom.is_empty:
        return Polygon()
    if isinstance(geom, Polygon | MultiPolygon):
        return geom
    if isinstance(geom, GeometryCollection):
        parts = [g for g in geom.geoms if isinstance(g, Polygon | MultiPolygon) and not g.is_empty]
        if not parts:
            return Polygon()
        return unary_union(parts)
    return Polygon()


def as_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    """Force a (valid) polygonal geometry into a MultiPolygon."""
    geom = polygons_only(make_valid(geom))
    if geom.is_empty:
        return MultiPolygon()
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    return geom


def mask_to_geometry(mask: np.ndarray, transform: Affine, *, connectivity: int = 4) -> BaseGeometry:
    """Polygonize a boolean mask into one (Multi)Polygon in the CRS of ``transform``."""
    m = np.ascontiguousarray(mask.astype(np.uint8))
    if not m.any():
        return Polygon()
    polys: list[BaseGeometry] = []
    for geom_json, value in features.shapes(
        m, mask=m.astype(bool), transform=transform, connectivity=connectivity
    ):
        if value != 1:
            continue
        g = make_valid(shape(geom_json))
        g = polygons_only(g)
        if not g.is_empty:
            polys.append(g)
    if not polys:
        return Polygon()
    return polygons_only(make_valid(unary_union(polys)))


def vectorize_masks(
    masks: Iterable[Mask],
    transform: Affine,
    tile_id: str,
    *,
    valid_mask: np.ndarray | None = None,
) -> list[Detection]:
    """Turn instance masks of one tile into Detections (metric CRS of the tile).

    ``valid_mask`` (True = data pixel) removes nodata padding from the masks first.
    """
    dets: list[Detection] = []
    for m in masks:
        arr = m.array if valid_mask is None else (m.array & valid_mask)
        geom = mask_to_geometry(arr, transform)
        if geom.is_empty or geom.area <= 0:
            continue
        dets.append(Detection(geom=geom, score=float(m.score), tile_id=tile_id))
    return dets


def pair_overlap(a: BaseGeometry, b: BaseGeometry) -> tuple[float, float]:
    """Return ``(iou, overlap_ratio)`` with ``overlap_ratio = inter / min(area_a, area_b)``."""
    inter = a.intersection(b).area
    if inter <= 0.0:
        return 0.0, 0.0
    union = a.area + b.area - inter
    iou = inter / union if union > 0 else 0.0
    ratio = inter / min(a.area, b.area) if min(a.area, b.area) > 0 else 0.0
    return float(iou), float(ratio)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[max(ri, rj)] = min(ri, rj)


def merge_overlaps(
    dets: Sequence[Detection],
    *,
    iou_threshold: float = 0.1,
    min_overlap_ratio: float = 0.5,
    score_mode: str = "max",
    merge_same_tile: bool = False,
) -> list[Detection]:
    """Merge detections split across tile overlaps (transitively, via union-find).

    Two detections are linked when they come from different tiles (or ``merge_same_tile``)
    and ``IoU >= iou_threshold`` or ``intersection / min(area) >= min_overlap_ratio``.
    A group becomes ``unary_union`` of its members; score is ``max`` or ``area_weighted``
    (``sum(score_k * area_k) / sum(area_k)``); ``tile_id`` is that of the largest member.
    """
    if score_mode not in {"max", "area_weighted"}:
        raise ValueError("score_mode must be 'max' or 'area_weighted'")
    n = len(dets)
    if n <= 1:
        return list(dets)
    geoms = [d.geom for d in dets]
    tree = STRtree(geoms)
    uf = _UnionFind(n)
    left, right = tree.query(geoms, predicate="intersects")
    for i, j in zip(left.tolist(), right.tolist(), strict=True):
        if j <= i:
            continue
        if not merge_same_tile and dets[i].tile_id == dets[j].tile_id:
            continue
        iou, ratio = pair_overlap(geoms[i], geoms[j])
        if iou >= iou_threshold or ratio >= min_overlap_ratio:
            uf.union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)
    merged: list[Detection] = []
    for root in sorted(groups):
        members = [dets[k] for k in groups[root]]
        if len(members) == 1:
            merged.append(members[0])
            continue
        geom = polygons_only(make_valid(unary_union([m.geom for m in members])))
        areas = np.array([m.area for m in members], dtype=float)
        scores = np.array([m.score for m in members], dtype=float)
        if score_mode == "max":
            score = float(scores.max())
        else:
            score = (
                float((scores * areas).sum() / areas.sum())
                if areas.sum() > 0
                else float(scores.max())
            )
        tile_id = members[int(np.argmax(areas))].tile_id
        merged.append(Detection(geom=geom, score=min(max(score, 0.0), 1.0), tile_id=tile_id))
    return merged


def simplify_detections(dets: Iterable[Detection], tolerance_m: float) -> list[Detection]:
    """Douglas-Peucker simplification (shapely ``simplify``, topology preserved), metres."""
    out: list[Detection] = []
    for d in dets:
        g = d.geom.simplify(tolerance_m, preserve_topology=True) if tolerance_m > 0 else d.geom
        g = polygons_only(make_valid(g))
        if not g.is_empty:
            out.append(Detection(geom=g, score=d.score, tile_id=d.tile_id))
    return out


def filter_min_area(dets: Iterable[Detection], min_area_m2: float) -> list[Detection]:
    """Drop detections whose area (metric CRS) is below ``min_area_m2``."""
    return [d for d in dets if d.area >= min_area_m2]


def to_geodataframe(
    dets: Sequence[Detection], crs: Any, model_version: str, *, to_wgs84: bool = True
) -> gpd.GeoDataFrame:
    """Detections -> GeoDataFrame with contract properties (§3.6), MultiPolygon, EPSG:4326."""
    records = [
        {
            "geometry": as_multipolygon(d.geom),
            "score": round(float(min(max(d.score, 0.0), 1.0)), 6),
            "model_version": model_version,
            "tile_id": d.tile_id,
        }
        for d in dets
    ]
    records = [r for r in records if not r["geometry"].is_empty]
    if not records:
        gdf = gpd.GeoDataFrame(
            {"score": [], "model_version": [], "tile_id": []},
            geometry=gpd.GeoSeries([], crs=crs),
        )
    else:
        gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
    if to_wgs84 and gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    gdf["geometry"] = gdf.geometry.apply(as_multipolygon) if len(gdf) else gdf.geometry
    return gdf.reset_index(drop=True)


def postprocess(
    dets: Sequence[Detection],
    vec_cfg: dict[str, Any] | None = None,
) -> list[Detection]:
    """merge -> simplify -> min-area filter, driven by ``config/cv.yaml -> vectorize``."""
    cfg = vec_cfg or {}
    merged = merge_overlaps(
        dets,
        iou_threshold=float(cfg.get("tile_overlap_merge_iou", 0.1)),
        min_overlap_ratio=float(cfg.get("min_overlap_ratio", 0.5)),
        score_mode=str(cfg.get("score_mode", "max")),
        merge_same_tile=bool(cfg.get("merge_same_tile", False)),
    )
    simplified = simplify_detections(merged, float(cfg.get("simplify_tolerance_m", 0.5)))
    return filter_min_area(simplified, float(cfg.get("min_area_m2", 12)))
