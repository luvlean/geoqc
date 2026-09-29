"""``geoqc cv threshold-sweep --split val``: F1@IoU>=0.5 over a grid of confidence thresholds.

Metrics come from ``geoqc.eval.detection_metrics`` when it is importable and behaves as
expected; otherwise from the local implementation of the formulas in docs/CONTRACTS.md §4
(greedy 1:1 matching by descending IoU; TP/FP/FN; P/R/F1; mean IoU of matched pairs).

The sweep is allowed on ``val`` only (optionally its ``holdout`` subset). Inference runs once
at ``sweep.conf_floor``; every threshold then filters the raw per-tile masks by score,
re-runs the tile-overlap merge / simplification / min-area filter and scores the result.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from shapely import STRtree
from shapely.geometry.base import BaseGeometry

from geoqc.config import resolve_path
from geoqc.cv.dataset import LABEL_SPLIT, load_labels, read_holdout_tile_ids
from geoqc.cv.pipeline import predict_tiles_raw, read_detections
from geoqc.cv.predictor import Predictor, build_predictor
from geoqc.cv.tiles import filter_by_split, load_tile_index, metric_crs_for
from geoqc.cv.vectorize import Detection, postprocess
from geoqc.cv.weights import current_model_version, update_cv_config

DEFAULT_THRESHOLDS = [round(0.05 * k, 2) for k in range(1, 19)]  # 0.05 .. 0.90


# --- local metric implementation (CONTRACTS §4) --------------------------------------------


def iou(a: BaseGeometry, b: BaseGeometry) -> float:
    inter = a.intersection(b).area
    if inter <= 0:
        return 0.0
    union = a.area + b.area - inter
    return float(inter / union) if union > 0 else 0.0


def match_one_to_one(
    gts: Sequence[BaseGeometry], preds: Sequence[BaseGeometry], iou_thr: float = 0.5
) -> list[tuple[int, int, float]]:
    """Greedy 1:1 matching by descending IoU; a pair counts when IoU >= iou_thr."""
    if not gts or not preds:
        return []
    tree = STRtree(list(preds))
    cands: list[tuple[float, int, int]] = []
    gi_arr, pi_arr = tree.query(list(gts), predicate="intersects")
    for gi, pi in zip(gi_arr.tolist(), pi_arr.tolist(), strict=True):
        v = iou(gts[gi], preds[pi])
        if v >= iou_thr:
            cands.append((v, gi, pi))
    cands.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_g: set[int] = set()
    used_p: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for v, gi, pi in cands:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        pairs.append((gi, pi, v))
    return pairs


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return float(p), float(r), float(f1)


def _try_eval_module() -> Any | None:
    try:
        from geoqc.eval import detection_metrics as dm  # type: ignore[import-not-found]
    except Exception:
        return None
    return dm if all(hasattr(dm, n) for n in ("match_one_to_one", "precision_recall_f1")) else None


def evaluate(
    gts: Sequence[BaseGeometry], preds: Sequence[BaseGeometry], iou_thr: float = 0.5
) -> dict[str, Any]:
    """TP/FP/FN, P/R/F1 and mean IoU of matched pairs; prefers geoqc.eval when available."""
    impl = "local"
    pairs: list[tuple[int, int, float]] | None = None
    dm = _try_eval_module()
    if dm is not None:
        try:
            res = dm.match_one_to_one(list(gts), list(preds), iou_thr)
            cand = [tuple(p) for p in getattr(res, "pairs", res)]  # MatchResult or plain pairs
            if all(len(p) == 3 for p in cand):
                pairs = [(int(a), int(b), float(c)) for a, b, c in cand]
                impl = "geoqc.eval.detection_metrics"
        except Exception:
            pairs = None
    if pairs is None:
        pairs = match_one_to_one(gts, preds, iou_thr)
    tp = len(pairs)
    fp = len(preds) - tp
    fn = len(gts) - tp
    p, r, f1 = precision_recall_f1(tp, fp, fn)
    mean_iou = float(np.mean([v for _, _, v in pairs])) if pairs else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(p, 6),
        "recall": round(r, 6),
        "f1": round(f1, 6),
        "mean_iou_matched": round(mean_iou, 6),
        "metrics_impl": impl,
    }


# --- sweep ------------------------------------------------------------------------------------


def sweep_thresholds(
    raw_by_tile: dict[str, list[Detection]],
    gts: Sequence[BaseGeometry],
    thresholds: Sequence[float],
    vec_cfg: dict[str, Any],
    iou_thr: float = 0.5,
) -> list[dict[str, Any]]:
    """Score every threshold: filter raw masks by score, postprocess, evaluate."""
    all_raw = [d for dets in raw_by_tile.values() for d in dets]
    rows: list[dict[str, Any]] = []
    for t in thresholds:
        kept = [d for d in all_raw if d.score >= t]
        final = postprocess(kept, vec_cfg)
        m = evaluate(gts, [d.geom for d in final], iou_thr)
        rows.append({"conf": float(t), "n_det": len(final), **m})
    return rows


def pick_best(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Highest F1; ties -> higher precision, then higher threshold (fewer false positives)."""
    if not rows:
        raise ValueError("empty sweep")
    return max(rows, key=lambda r: (r["f1"], r["precision"], r["conf"]))


def run_sweep(
    cv_cfg: dict[str, Any],
    pipeline_cfg: dict[str, Any] | None = None,
    *,
    split: str = LABEL_SPLIT,
    holdout_only: bool = False,
    detections_path: str | Path | None = None,
    thresholds: Sequence[float] | None = None,
    labels_path: str | Path | None = None,
    split_csv: str | Path | None = None,
    tiles_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    dataset_dir: str | Path | None = None,
    predictor: Predictor | None = None,
    device: str | None = None,
    backend: str | None = None,
    limit: int | None = None,
    require_verified: bool = True,
    update_config: bool = True,
    config_path: str | Path | None = None,
    report_path: str | Path | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    """Threshold sweep on val (or its holdout part) -> report json (+ conf_threshold in config)."""
    if split != LABEL_SPLIT:
        raise ValueError(f"threshold sweep is allowed on split {LABEL_SPLIT!r} only")
    sw = dict(cv_cfg.get("sweep") or {})
    thresholds = sorted(
        float(t) for t in (thresholds or sw.get("thresholds") or DEFAULT_THRESHOLDS)
    )
    iou_thr = float(sw.get("iou_threshold", 0.5))
    conf_floor = float(sw.get("conf_floor", 0.05))
    vec_cfg = dict(cv_cfg.get("vectorize") or {})
    paths = (pipeline_cfg or {}).get("paths", {}) if pipeline_cfg else {}
    tiles_dir = tiles_dir or paths.get("tiles", "data/tiles")
    index_path = index_path or ((pipeline_cfg or {}).get("tiles", {}) or {}).get("index")

    labels_root = Path(paths.get("labels", "data/labels"))
    split_csv = split_csv or labels_root / "tiles_split.csv"
    labels_path = labels_path or labels_root / LABEL_SPLIT / "buildings.geojson"
    index = filter_by_split(load_tile_index(tiles_dir, index_path), split, split_csv)
    subset = "val"
    if holdout_only:
        hold = set(
            read_holdout_tile_ids(dataset_dir or (cv_cfg.get("dataset") or {}).get("out_dir"))
        )
        index = index[index["tile_id"].isin(hold)].reset_index(drop=True)
        subset = "val/holdout"
    if limit is not None:
        index = index.head(int(limit)).reset_index(drop=True)
    if len(index) == 0:
        raise ValueError("no tiles selected for the sweep")

    labels = load_labels(labels_path, require_verified=require_verified)
    metric_crs = None
    raw_by_tile: dict[str, list[Detection]]
    if detections_path is not None:
        dets = read_detections(detections_path)
        dets = (
            dets[dets["tile_id"].astype(str).isin(set(index["tile_id"]))]
            if "tile_id" in dets.columns
            else dets
        )
        metric_crs = metric_crs_for(None, index)
        dets_m = dets.to_crs(metric_crs) if len(dets) else dets
        raw_by_tile = {
            "_file": [
                Detection(g, float(s), str(t))
                for g, s, t in zip(dets_m.geometry, dets_m["score"], dets_m["tile_id"], strict=True)
            ]
        }
        # detections from a file are already merged: skip the merge step but keep filters
        vec_cfg = {**vec_cfg, "tile_overlap_merge_iou": 2.0, "min_overlap_ratio": 2.0}
        model_version = (
            str(dets["model_version"].iloc[0]) if len(dets) else current_model_version(cv_cfg)
        )
        source = str(detections_path)
    else:
        if predictor is None:
            predictor = build_predictor(cv_cfg, device=device, conf=conf_floor, backend=backend)
        raw_by_tile, metric_crs, _, _ = predict_tiles_raw(
            predictor,
            index,
            batch=int(cv_cfg.get("batch", 8)),
            drop_nodata=bool(vec_cfg.get("drop_nodata", True)),
            progress=progress,
        )
        model_version = (
            current_model_version(cv_cfg) if predictor.name != "mock" else "mock-00000000"
        )
        source = f"inference@conf_floor={conf_floor}"
    if metric_crs is None:
        metric_crs = metric_crs_for(None, index)

    tiles_union = index.geometry.union_all()
    gt_sel = labels[labels.intersects(tiles_union)]
    gts = list(gt_sel.to_crs(metric_crs).geometry)

    rows = sweep_thresholds(raw_by_tile, gts, thresholds, vec_cfg, iou_thr)
    best = pick_best(rows)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    report = {
        "split": split,
        "subset": subset,
        "generated_at": now,
        "model_version": model_version,
        "backend": backend or cv_cfg.get("backend"),
        "iou_threshold": iou_thr,
        "n_tiles": int(len(index)),
        "n_gt": len(gts),
        "detections_source": source,
        "metrics_impl": rows[0]["metrics_impl"] if rows else "local",
        "vectorize": vec_cfg,
        "thresholds": rows,
        "best": {
            "conf": best["conf"],
            "f1": best["f1"],
            "precision": best["precision"],
            "recall": best["recall"],
        },
    }
    out = Path(resolve_path(report_path or sw.get("report", "reports/threshold_sweep_val.json")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["report_path"] = str(out)
    if update_config:
        update_cv_config(
            {"conf_threshold": float(best["conf"]), "conf_threshold_split": f"{subset} @ {now}"},
            config_path=config_path,
        )
    return report
