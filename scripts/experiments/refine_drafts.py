"""Model-free contour refinement for annotation drafts: GrabCut on the 7 cm source image.

Each draft polygon seeds GrabCut in a window around it: pixels farther than BAND m outside are
background, the draft eroded by BAND m is foreground, the band in between is "probable". The
largest refined component is kept when it stays close to the draft (IoU >= MIN_IOU, area ratio in
[0.7, 1.4]); otherwise the draft is kept and flagged. No detector output is used, so test labels
stay independent of the model. Touching refined polygons (gap <= MERGE_GAP m) are merged: sections
of one building are one polygon (D-016). Usage: python refine_drafts.py <split> <out.gpkg> [--from-draft]
"""

from __future__ import annotations

import sys

import cv2
import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
from rasterio import features
from rasterio.windows import Window, from_bounds
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union

RAW = "data/raw/openaerialmap_6101142e5a90f10006fd3038.tif"
BAND, MARGIN, MIN_IOU, MERGE_GAP, SIMPLIFY = 1.2, 4.0, 0.6, 0.3, 0.15


def refine_one(src: rasterio.DatasetReader, poly: Polygon) -> tuple[Polygon, str]:
    minx, miny, maxx, maxy = poly.bounds
    win = from_bounds(minx - MARGIN, miny - MARGIN, maxx + MARGIN, maxy + MARGIN, src.transform)
    # boundless reads crash this GDAL build: clip the window to the image instead
    win = win.intersection(Window(0, 0, src.width, src.height)).round_offsets().round_lengths()
    img = np.transpose(src.read([1, 2, 3], window=win), (1, 2, 0)).astype(np.uint8)
    tr = src.window_transform(win)
    px = abs(tr.a)
    h, w = img.shape[:2]
    if h < 8 or w < 8:
        return poly, "draft kept: window too small"
    draft = features.rasterize([(poly, 1)], out_shape=(h, w), transform=tr).astype(np.uint8)
    k = max(1, int(round(BAND / px)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
    outer, inner = cv2.dilate(draft, kernel), cv2.erode(draft, kernel)
    mask = np.full((h, w), cv2.GC_BGD, np.uint8)
    mask[outer == 1] = cv2.GC_PR_BGD
    mask[draft == 1] = cv2.GC_PR_FGD
    mask[inner == 1] = cv2.GC_FGD
    if not (mask == cv2.GC_FGD).any():
        mask[draft == 1] = cv2.GC_FGD  # tiny draft: no eroded core
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(img[:, :, ::-1].copy(), mask, None, bgd, fgd, 5, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return poly, "draft kept: grabcut failed"
    fg = np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, cc, stats, _ = cv2.connectedComponentsWithStats(fg)
    if n <= 1:
        return poly, "draft kept: nothing segmented"
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    comp = (cc == best).astype(np.uint8)
    shapes = [shape(g) for g, v in features.shapes(comp, mask=comp == 1, transform=tr) if v == 1]
    if not shapes:
        return poly, "draft kept: no polygon"
    ref = max(shapes, key=lambda g: g.area)
    ref = Polygon(ref.exterior).simplify(SIMPLIFY, preserve_topology=True)  # roofs: no holes
    iou = ref.intersection(poly).area / ref.union(poly).area
    ratio = ref.area / poly.area
    if iou < MIN_IOU or not 0.7 <= ratio <= 1.4:
        return poly, f"draft kept: refinement drifted (IoU {iou:.2f}, area x{ratio:.2f})"
    return ref, f"refined (IoU to draft {iou:.2f})"


def main(split: str, out: str, from_draft: bool = False) -> None:
    src = rasterio.open(RAW)
    if from_draft:  # calibration: refine the drafts of a split that already has human labels
        df = pyogrio.read_dataframe(f"data/labels/{split}/draft.geojson")
    else:
        df = pyogrio.read_dataframe(f"data/labels/{split}/work.gpkg", layer="buildings")
    crs = df.crs
    df = df.to_crs(src.crs)
    rows = []
    for r in df.itertuples():
        geoms = list(r.geometry.geoms) if r.geometry.geom_type == "MultiPolygon" else [r.geometry]
        for g in geoms:
            ref, note = refine_one(src, g)
            rows.append({"tile_id": r.tile_id, "geometry": ref, "note": note, "draft_fid": r.Index})
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=src.crs)
    merged = []
    for tile, grp in gdf.groupby("tile_id"):
        clusters = unary_union([g.buffer(MERGE_GAP / 2) for g in grp.geometry])
        parts = list(clusters.geoms) if clusters.geom_type == "MultiPolygon" else [clusters]
        for part in parts:
            members = grp[grp.geometry.buffer(MERGE_GAP / 2).intersects(part)]
            geom = unary_union(list(members.geometry)).buffer(MERGE_GAP / 2).buffer(-MERGE_GAP / 2)
            if geom.geom_type == "MultiPolygon":
                geom = max(geom.geoms, key=lambda x: x.area)
            note = "; ".join(sorted(set(members["note"])))
            if len(members) > 1:
                note = f"merged {len(members)} drafts (D-016); " + note
            merged.append(
                {
                    "tile_id": tile,
                    "geometry": Polygon(geom.exterior),
                    "note": note,
                    "draft_fids": ",".join(map(str, members["draft_fid"])),
                }
            )
    res = gpd.GeoDataFrame(merged, geometry="geometry", crs=src.crs).to_crs(crs)
    res.to_file(out, driver="GPKG", layer="refined")
    print(
        res["note"].str.split(";").str[0].str.split("(").str[0].str.strip().value_counts().to_dict()
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], "--from-draft" in sys.argv[3:])
