"""RID2 (Netherlands, 8 cm, roof outlines, CC BY 4.0) -> hard small-building chips at our 0.3 m scale.

1. windows 512 px x 0.3 m (153.6 m), stride half a window, >= 60 % covered by RID2 images
   (uncovered part is black, like nodata at our AOI edge);
2. drop windows touching community gardens (OSM landuse=allotments): RID2 does not label their
   garden houses, which would teach the model to ignore our dachas;
3. keep windows with >= MIN_SMALL buildings of 12-100 m2;
4. hard-example mining: the current model (train + AIRS) runs on every window; windows are ranked by
   the number of small buildings it misses (mask IoU < 0.5) and the top N_HARD are kept;
5. two datasets from the same windows (labels are polygons from gdf_all_outlines.json):
     masked/ - buildings >= 100 m2 painted black and left unlabelled (learn only the small ones),
     full/   - original image, every building >= 12 m2 labelled (control).
Usage: RID2_DIR=... python scripts/experiments/rid2_prepare.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio import features
from rasterio.merge import merge
from rasterio.transform import from_origin
from shapely.geometry import Polygon

from geoqc.config import load_config
from geoqc.data.osm import fetch_overpass

REPO = Path.cwd()
RID2 = Path(os.environ.get("RID2_DIR", Path.home() / "Downloads/roof_information_dataset_2"))
OUT = REPO / "data/interim/external/rid2"
WEIGHTS = REPO / "models/yolov8m-building-seg-ft-ours-airs.pt"
GSD, CHIP, MIN_COVER = 0.3, 512, 0.6
SMALL_MIN, SMALL_MAX, MIN_SMALL, N_HARD = 12.0, 100.0, 10, 400
LOG = OUT / "prepare.log"


def log(msg: str) -> None:
    print(msg, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def allotments(wins: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    cache = OUT / "allotments.gpkg"
    if cache.exists():
        return gpd.read_file(cache)
    # Small boxes around the windows only, in batches: one query for all 50 regions timed out on
    # every public Overpass endpoint.
    groups = wins.geometry.buffer(50).union_all()
    parts = list(getattr(groups, "geoms", [groups]))
    bboxes = list(gpd.GeoSeries(parts, crs=28992).to_crs(4326).bounds.itertuples())
    ocfg = load_config()["data"][
        "overpass"
    ]  # same endpoints, user agent and retries as `geoqc data osm`
    elements = []
    for k in range(0, len(bboxes), 25):
        q = "".join(
            f'way["landuse"="allotments"]({r.miny},{r.minx},{r.maxy},{r.maxx});'
            f'relation["landuse"="allotments"]({r.miny},{r.minx},{r.maxy},{r.maxx});'
            for r in bboxes[k : k + 25]
        )
        data = fetch_overpass(
            f"[out:json][timeout:120];({q});out geom;",
            ocfg["endpoints"],
            timeout_s=150,
            retries=ocfg["retries"],
            backoff_s=ocfg["backoff_s"],
            user_agent=ocfg["user_agent"],
        )
        elements += data["elements"]
        log(
            f"allotments: batch {k // 25 + 1}/{(len(bboxes) + 24) // 25}, {len(data['elements'])} elements"
        )
    data = {"elements": elements}
    polys = []
    for el in data["elements"]:
        rings = (
            [el["geometry"]]
            if el["type"] == "way"
            else [m["geometry"] for m in el.get("members", []) if m.get("role") == "outer"]
        )
        for ring in rings:
            coords = [(p["lon"], p["lat"]) for p in ring]
            if len(coords) >= 4:
                poly = Polygon(coords)
                polys.append(poly if poly.is_valid else poly.buffer(0))
    gdf = gpd.GeoDataFrame(geometry=polys, crs=4326).to_crs(28992)
    gdf.to_file(cache, driver="GPKG")
    return gdf


def render(win, idx: gpd.GeoDataFrame) -> np.ndarray:
    srcs = [rasterio.open(p) for p in idx[idx.intersects(win)].path]
    mos, _ = merge(srcs, bounds=win.bounds, res=0.08)
    for s in srcs:
        s.close()
    img = np.transpose(mos[:3], (1, 2, 0))
    return cv2.resize(img, (CHIP, CHIP), interpolation=cv2.INTER_AREA)


def instance_masks(polys, win) -> list[np.ndarray]:
    tr = from_origin(win.bounds[0], win.bounds[3], GSD, GSD)
    return [
        features.rasterize([(p, 1)], out_shape=(CHIP, CHIP), transform=tr).astype(bool)
        for p in polys
    ]


def yolo_lines(polys, win) -> list[str]:
    x0, y1 = win.bounds[0], win.bounds[3]
    lines = []
    for p in polys:
        for g in getattr(p, "geoms", [p]):
            if g.geom_type != "Polygon" or g.area < SMALL_MIN:
                continue
            xy = np.asarray(g.exterior.coords)[:-1]
            px = np.clip((xy[:, 0] - x0) / GSD / CHIP, 0, 1)
            py = np.clip((y1 - xy[:, 1]) / GSD / CHIP, 0, 1)
            lines.append("0 " + " ".join(f"{a:.6f} {b:.6f}" for a, b in zip(px, py, strict=True)))
    return lines


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    wins = gpd.read_file(OUT / "windows.gpkg")
    idx = gpd.read_file(OUT / "tiles_index.gpkg")
    outl = gpd.read_file(RID2 / "geometries/gdf_all_outlines.json")
    # a few RID2 outlines self-intersect; clipping them to a window raises a GEOS topology error
    outl["geometry"] = outl.geometry.make_valid()
    outl = outl.explode(index_parts=False)
    outl = outl[outl.geom_type == "Polygon"]
    outl["a"] = outl.area
    outl = outl[outl.a >= SMALL_MIN].reset_index(drop=True)
    allot = allotments(wins)
    near = wins.intersects(allot.union_all()) if len(allot) else pd.Series(False, index=wins.index)
    cand = wins[~near & (wins.n_small >= MIN_SMALL)].reset_index(drop=True)
    log(
        f"windows: {len(wins)}; touching allotments: {int(near.sum())} ({len(allot)} allotment polygons); "
        f"candidates (>= {MIN_SMALL} small, no allotments): {len(cand)}"
    )

    from ultralytics import YOLO

    model = YOLO(str(WEIGHTS))
    rows = []
    chips_dir = OUT / "chips"
    chips_dir.mkdir(exist_ok=True)
    for i, w in enumerate(cand.geometry):
        img = render(w, idx)
        cv2.imwrite(str(chips_dir / f"w{i:04d}.png"), img[:, :, ::-1])
        inside = outl[outl.intersects(w)]
        clipped = inside.geometry.intersection(w)
        small = [
            g
            for g, a in zip(clipped, inside.a, strict=True)
            if a < SMALL_MAX and g.area >= SMALL_MIN
        ]
        res = model.predict(
            img[:, :, ::-1].copy(),
            imgsz=768,
            conf=0.4,
            retina_masks=True,
            verbose=False,
            device="mps",
        )[0]
        pred = (
            res.masks.data.cpu().numpy().astype(bool)
            if res.masks is not None
            else np.zeros((0, CHIP, CHIP), bool)
        )
        missed = 0
        for gm in instance_masks(small, w):
            ious = [(gm & pm).sum() / max((gm | pm).sum(), 1) for pm in pred]
            missed += int(max(ious, default=0) < 0.5)
        rows.append({"win": i, "n_small": len(small), "missed_small": missed})
        if i % 50 == 0:
            log(f"mined {i}/{len(cand)}")
    mined = pd.DataFrame(rows)
    mined["miss_rate"] = mined.missed_small / mined.n_small.clip(lower=1)
    mined = mined.sort_values(["missed_small", "miss_rate"], ascending=False)
    hard = mined.head(N_HARD)
    mined.to_csv(OUT / "mining.csv", index=False)
    log(
        f"current model misses {int(mined.missed_small.sum())} of {int(mined.n_small.sum())} small buildings "
        f"({mined.missed_small.sum() / max(mined.n_small.sum(), 1):.1%}); hard windows kept: {len(hard)} "
        f"with {int(hard.n_small.sum())} small buildings, {int(hard.missed_small.sum())} missed"
    )

    for variant in ("masked", "full"):
        for kind in ("images", "labels"):
            (OUT / variant / kind / "train").mkdir(parents=True, exist_ok=True)
    for r in hard.itertuples():
        w = cand.geometry.iloc[r.win]
        img = cv2.imread(str(chips_dir / f"w{r.win:04d}.png"))[:, :, ::-1].copy()
        inside = outl[outl.intersects(w)]
        clipped = list(inside.geometry.intersection(w))
        areas = list(inside.a)
        big = [g.buffer(1.0) for g, a in zip(clipped, areas, strict=True) if a >= SMALL_MAX]
        small = [g for g, a in zip(clipped, areas, strict=True) if a < SMALL_MAX]
        name = f"rid2_w{r.win:04d}"
        cv2.imwrite(str(OUT / "full/images/train" / f"{name}.png"), img[:, :, ::-1])
        (OUT / "full/labels/train" / f"{name}.txt").write_text(
            "\n".join(yolo_lines(clipped, w)) + "\n"
        )
        masked = img.copy()
        if big:
            masked[np.any(instance_masks(big, w), axis=0)] = (
                0  # unseen, unlabelled: no "roof = background" lesson
            )
        cv2.imwrite(str(OUT / "masked/images/train" / f"{name}.png"), masked[:, :, ::-1])
        (OUT / "masked/labels/train" / f"{name}.txt").write_text(
            "\n".join(yolo_lines(small, w)) + "\n"
        )
    meta = {
        "source": "RID2 (Krapf et al., TUM), Beeldmateriaal Nederland, CC BY 4.0",
        "gsd_m": GSD,
        "windows": len(wins),
        "candidates": len(cand),
        "hard": len(hard),
        "small_buildings_hard": int(hard.n_small.sum()),
        "missed_by_current_model": int(hard.missed_small.sum()),
        "excluded_allotment_windows": int(near.sum()),
    }
    (OUT / "prepared.json").write_text(json.dumps(meta, indent=2))
    log(f"prepared {meta}")


if __name__ == "__main__":
    main()
