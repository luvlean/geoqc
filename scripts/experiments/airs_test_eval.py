"""Score models on the AIRS test part (Christchurch): images the models never saw, labelled by the AIRS authors.

Chips as in airs_night.py: 7.5 cm -> 0.3 m (average resampling, x4), 512 px grid, >= 90 % data;
unlike training, chips without buildings are kept (false detections count). Ground truth: connected
components of the roof mask >= 12 m2 (touching roofs form one object, as in training).
Detections: geoqc predictor + postprocess (simplify, >= 12 m2) at the lowest threshold, then scored per
threshold; 1:1 greedy matching per chip, IoU >= 0.5, plain and area-weighted P/R/F1 (D-023).
Result: reports/experiments/airs_test.json. Not a gate: AIRS train was part of the training data,
so this measures unseen Christchurch tiles, not our AOI.
Usage: AIRS_DIR=... python scripts/experiments/airs_test_eval.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np
import rasterio
from affine import Affine
from rasterio.enums import Resampling

from geoqc.config import load_config
from geoqc.cv.predictor import UltralyticsSegPredictor
from geoqc.cv.vectorize import mask_to_geometry, postprocess, vectorize_masks
from geoqc.eval.detection_metrics import match_one_to_one, mean_iou

REPO = Path.cwd()
RAW = Path(os.environ.get("AIRS_DIR", REPO / "data/raw/external/airs")) / "test"
OUT = REPO / "reports/experiments/airs_test.json"
CHIP, FACTOR, GSD, MIN_AREA = 512, 4, 0.3, 12.0
MODELS = {  # name -> (weights, threshold chosen on our val by F1_area)
    "base_768": ("models/yolov8m-building-seg.pt", 0.30),
    "airs_768": ("models/yolov8m-building-seg-ft-ours-airs.pt", 0.50),
}
THRESHOLDS = (0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6)
BINS, CLASSES = (12, 30, 100, 500, np.inf), ("12–30", "30–100", "100–500", ">500")


def read_down(path: Path) -> np.ndarray:
    with rasterio.open(path) as src:
        h, w = src.height // FACTOR, src.width // FACTOR
        return src.read(out_shape=(src.count, h, w), resampling=Resampling.average)


def chips() -> list[tuple[str, np.ndarray, list]]:
    out = []
    for img_p in sorted((RAW / "image").glob("*.tif")):
        img = np.transpose(read_down(img_p)[:3], (1, 2, 0)).astype(np.uint8)
        lab = read_down(RAW / "label" / img_p.name)[0]
        mask = lab > (lab.max() / 2 if lab.max() > 0 else 1)
        for r in range(0, img.shape[0] - CHIP + 1, CHIP):
            for c in range(0, img.shape[1] - CHIP + 1, CHIP):
                chip = img[r : r + CHIP, c : c + CHIP]
                if np.mean(np.any(chip > 0, axis=2)) < 0.9:
                    continue
                n, cc = cv2.connectedComponents(
                    mask[r : r + CHIP, c : c + CHIP].astype(np.uint8), connectivity=8
                )
                gt = [mask_to_geometry(cc == k, Affine(GSD, 0, 0, 0, GSD, 0)) for k in range(1, n)]
                out.append((f"{img_p.stem}_{r}_{c}", chip, [g for g in gt if g.area >= MIN_AREA]))
    return out


def size_class(area: float) -> str:
    return CLASSES[int(np.digitize(area, BINS[1:-1]))]


def score(data, dets_per_chip, thr: float) -> dict:
    tp = fp = fn = 0
    pairs_iou, gt_a, det_a, gt_hit, det_hit = [], [], [], [], []
    for (_, _, gt), dets in zip(data, dets_per_chip, strict=True):
        pred = [d.geom for d in dets if d.score >= thr]
        m = match_one_to_one(gt, pred, 0.5)
        tp, fp, fn = tp + m.tp, fp + m.fp, fn + m.fn
        pairs_iou += [i for _, _, i in m.pairs]
        hit_g, hit_p = {g for g, _, _ in m.pairs}, {p for _, p, _ in m.pairs}
        gt_a += [g.area for g in gt]
        det_a += [p.area for p in pred]
        gt_hit += [i in hit_g for i in range(len(gt))]
        det_hit += [j in hit_p for j in range(len(pred))]
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    pa = float(np.dot(det_a, det_hit) / sum(det_a)) if det_a else 0.0
    ra = float(np.dot(gt_a, gt_hit) / sum(gt_a)) if gt_a else 0.0
    fa = 2 * pa * ra / (pa + ra) if pa + ra else 0.0  # = area_weighted_prf over all chips (D-023)
    gcls = np.array([size_class(a) for a in gt_a])
    recall_by_size = {
        k: {"n": int((gcls == k).sum()), "recall": float(np.mean(np.array(gt_hit)[gcls == k]))}
        for k in CLASSES
        if (gcls == k).any()
    }
    return {
        "conf": thr,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": p,
        "recall": r,
        "f1": 2 * p * r / (p + r) if p + r else 0.0,
        "precision_area": pa,
        "recall_area": ra,
        "f1_area": fa,
        "mean_iou_matched": mean_iou(pairs_iou),
        "recall_by_size": recall_by_size,
    }


def main() -> None:
    cv = load_config()["cv"]
    data = chips()
    n_gt = sum(len(g) for _, _, g in data)
    print(f"AIRS test: {len(data)} chips, {n_gt} buildings", flush=True)
    report = {
        "source": "AIRS test part (Chen et al. 2019), LINZ CC BY 4.0, Christchurch 7.5 cm -> 0.3 m",
        "images": len(list((RAW / "image").glob("*.tif"))),
        "chips": len(data),
        "chips_without_buildings": sum(not g for _, _, g in data),
        "buildings": n_gt,
        "models": {},
    }
    for name, (weights, chosen) in MODELS.items():
        pred = UltralyticsSegPredictor(
            weights, conf=min(THRESHOLDS), nms_iou=float(cv["nms_iou"]), imgsz=768, max_det=300
        )
        dets = []
        for k in range(0, len(data), 8):
            batch = data[k : k + 8]
            for (cid, _, _), masks in zip(
                batch, pred.predict_batch([ch for _, ch, _ in batch]), strict=True
            ):
                dets.append(
                    postprocess(
                        vectorize_masks(masks, Affine(GSD, 0, 0, 0, GSD, 0), cid), cv["vectorize"]
                    )
                )
            if k % 400 == 0:
                print(f"{name}: {k}/{len(data)} chips", flush=True)
        rows = [score(data, dets, t) for t in sorted({*THRESHOLDS, chosen})]
        at = next(r for r in rows if r["conf"] == chosen)
        report["models"][name] = {
            "weights": weights,
            "threshold": chosen,
            "at_threshold": at,
            "thresholds": rows,
        }
        print(
            f"{name} @ {chosen}: F1={at['f1']:.3f} F1_area={at['f1_area']:.3f} "
            f"P={at['precision']:.3f} R={at['recall']:.3f} IoU={at['mean_iou_matched']:.3f}",
            flush=True,
        )
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"written {OUT}")


if __name__ == "__main__":
    main()
