"""Detection metrics (CONTRACTS §4): IoU, 1:1 matching, P/R/F1, mean IoU, per-split evaluation.

Pure functions work on shapely geometries in a metric CRS. ``evaluate_geodataframes`` adds the
tile logic of a split (GT filtered to the split tiles, detections clipped to their union) and
``evaluate_split`` wires the files of CONTRACTS §3.5/§3.6 to it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import STRtree
from shapely.geometry.base import BaseGeometry

from geoqc.config import resolve_path
from geoqc.eval.common import (
    EvalInputError,
    EvalPaths,
    clean_polygons,
    load_eval_config,
    read_geojson,
    read_tiles_index,
    read_tiles_split,
    rel,
    resolve_metric_crs,
)

MATCH_METHODS: tuple[str, ...] = ("greedy", "hungarian")


# --------------------------------------------------------------------------- pure functions
def iou(a: BaseGeometry | None, b: BaseGeometry | None) -> float:
    """IoU(A,B) = area(A∩B) / area(A∪B); 0 for empty or non-overlapping inputs."""
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    inter = a.intersection(b).area
    if inter <= 0.0:
        return 0.0
    union = a.area + b.area - inter
    return float(inter / union) if union > 0.0 else 0.0


def iou_matrix(gts: Sequence[BaseGeometry], preds: Sequence[BaseGeometry]) -> np.ndarray:
    """Dense |gts| x |preds| IoU matrix; only intersecting pairs (STRtree) are computed."""
    matrix = np.zeros((len(gts), len(preds)), dtype=float)
    if len(gts) == 0 or len(preds) == 0:
        return matrix
    tree = STRtree(list(preds))
    for gi, g in enumerate(gts):
        if g is None or g.is_empty:
            continue
        for pi in tree.query(g, predicate="intersects"):
            matrix[gi, int(pi)] = iou(g, preds[int(pi)])
    return matrix


@dataclass
class MatchResult:
    """1:1 assignment between ground truth and predictions."""

    pairs: list[tuple[int, int, float]] = field(default_factory=list)  # (gt_idx, pred_idx, iou)
    unmatched_gt: list[int] = field(default_factory=list)
    unmatched_pred: list[int] = field(default_factory=list)
    iou_threshold: float = 0.5
    method: str = "greedy"

    @property
    def tp(self) -> int:
        return len(self.pairs)

    @property
    def fp(self) -> int:
        return len(self.unmatched_pred)

    @property
    def fn(self) -> int:
        return len(self.unmatched_gt)

    def ious(self) -> list[float]:
        return [p[2] for p in self.pairs]


def _greedy(matrix: np.ndarray, thr: float) -> list[tuple[int, int, float]]:
    gi_idx, pi_idx = np.nonzero(matrix >= thr)
    if gi_idx.size == 0:
        return []
    values = matrix[gi_idx, pi_idx]
    # Sort by IoU desc, then by indices for determinism on ties.
    order = np.lexsort((pi_idx, gi_idx, -values))
    used_g: set[int] = set()
    used_p: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for k in order:
        g, p = int(gi_idx[k]), int(pi_idx[k])
        if g in used_g or p in used_p:
            continue
        used_g.add(g)
        used_p.add(p)
        pairs.append((g, p, float(values[k])))
    return pairs


def _hungarian(matrix: np.ndarray, thr: float) -> list[tuple[int, int, float]]:
    from scipy.optimize import linear_sum_assignment

    eligible = np.where(matrix >= thr, matrix, 0.0)
    if not eligible.any():
        return []
    rows, cols = linear_sum_assignment(eligible, maximize=True)
    pairs = [
        (int(g), int(p), float(matrix[g, p]))
        for g, p in zip(rows, cols, strict=True)
        if matrix[g, p] >= thr
    ]
    return sorted(pairs, key=lambda t: (-t[2], t[0], t[1]))


def match_one_to_one(
    gts: Sequence[BaseGeometry],
    preds: Sequence[BaseGeometry],
    iou_thr: float = 0.5,
    method: str = "greedy",
    matrix: np.ndarray | None = None,
) -> MatchResult:
    """Match GT and predictions 1:1; a pair counts only when IoU >= ``iou_thr``.

    ``greedy``: pairs in IoU-descending order, each object used at most once.
    ``hungarian``: assignment maximising the total IoU over eligible pairs (scipy).
    """
    if method not in MATCH_METHODS:
        raise ValueError(f"unknown match method {method!r}; expected one of {MATCH_METHODS}")
    if matrix is None:
        matrix = iou_matrix(gts, preds)
    pairs = _greedy(matrix, iou_thr) if method == "greedy" else _hungarian(matrix, iou_thr)
    matched_g = {g for g, _, _ in pairs}
    matched_p = {p for _, p, _ in pairs}
    return MatchResult(
        pairs=pairs,
        unmatched_gt=[i for i in range(len(gts)) if i not in matched_g],
        unmatched_pred=[j for j in range(len(preds)) if j not in matched_p],
        iou_threshold=iou_thr,
        method=method,
    )


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """P = TP/(TP+FP), R = TP/(TP+FN), F1 = 2PR/(P+R); each is 0.0 when its denominator is 0."""
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return float(precision), float(recall), float(f1)


def mean_iou(pairs: Iterable[tuple[int, int, float]] | Iterable[float]) -> float:
    """Mean IoU over matched pairs (tuples ``(gt, pred, iou)`` or plain floats); 0.0 if empty."""
    values = [p[2] if isinstance(p, tuple | list) else float(p) for p in pairs]
    return float(np.mean(values)) if values else 0.0


def area_weighted_prf(
    match: MatchResult | Sequence[tuple[int, int, float]],
    gt_areas: Sequence[float],
    det_areas: Sequence[float],
) -> tuple[float, float, float]:
    """Area-weighted P/R/F1 (D-023): every building counts by its area, so a missed apartment
    block weighs more than a missed shed. P = matched detection area / all detection area,
    R = matched ground-truth area / all ground-truth area, F1 = their harmonic mean."""
    pairs = match.pairs if isinstance(match, MatchResult) else list(match)
    gt_a, det_a = np.asarray(gt_areas, float), np.asarray(det_areas, float)
    p = float(det_a[[q for _, q, _ in pairs]].sum() / det_a.sum()) if det_a.sum() > 0 else 0.0
    r = float(gt_a[[g for g, _, _ in pairs]].sum() / gt_a.sum()) if gt_a.sum() > 0 else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def detection_summary(
    match: MatchResult,
    gt_areas: Sequence[float] | None = None,
    det_areas: Sequence[float] | None = None,
) -> dict[str, Any]:
    """The ``detection.<split>`` block of metrics.json (CONTRACTS §3.9); with areas also the
    area-weighted P/R/F1 used to choose the model and threshold (D-023)."""
    p, r, f1 = precision_recall_f1(match.tp, match.fp, match.fn)
    out = {
        "tp": match.tp,
        "fp": match.fp,
        "fn": match.fn,
        "precision": p,
        "recall": r,
        "f1": f1,
        "mean_iou_matched": mean_iou(match.pairs),
    }
    if gt_areas is not None and det_areas is not None:
        pw, rw, fw = area_weighted_prf(match, gt_areas, det_areas)
        out.update(precision_area=pw, recall_area=rw, f1_area=fw)
    return out


# --------------------------------------------------------------------------- split evaluation
@dataclass
class SplitEvaluation:
    """Result of evaluating one split; consumed by ``detection`` and ``error-analysis``."""

    split: str
    metric_crs: str
    metric_crs_source: str
    gt: gpd.GeoDataFrame  # metric CRS; columns: tile_id, stratum, geometry (+ label properties)
    det: gpd.GeoDataFrame  # metric CRS, clipped; columns: tile_id, stratum, score, geometry
    tiles: gpd.GeoDataFrame | None  # metric CRS; columns: tile_id, stratum, geometry
    match: MatchResult
    iou_matrix: np.ndarray
    tile_ids: list[str]
    strata: dict[str, str]
    notes: list[str] = field(default_factory=list)
    labels_verified: bool = True
    labels_path: str | None = None
    detections_path: str | None = None
    model_version: str | None = None
    conf_threshold: float | None = None
    n_det_total: int | None = None

    # -- derived tables -----------------------------------------------------
    def summary(self) -> dict[str, Any]:
        return detection_summary(
            self.match,
            self.gt.geometry.area.to_numpy() if len(self.gt) else [],
            self.det.geometry.area.to_numpy() if len(self.det) else [],
        )

    def gt_status(self) -> pd.Series:
        """Per GT row: 'tp' or 'fn'."""
        status = pd.Series("fn", index=self.gt.index, dtype=object)
        for g, _, _ in self.match.pairs:
            status.iloc[g] = "tp"
        return status

    def det_status(self) -> pd.Series:
        """Per detection row: 'tp' or 'fp'."""
        status = pd.Series("fp", index=self.det.index, dtype=object)
        for _, p, _ in self.match.pairs:
            status.iloc[p] = "tp"
        return status

    def pair_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.match.pairs, columns=["gt_idx", "det_idx", "iou"])

    def per_tile(self) -> pd.DataFrame:
        """tp/fp/fn/P/R/F1/mean IoU per tile; TP and FN by the GT tile, FP by the detection tile."""
        rows: list[dict[str, Any]] = []
        gt_status = self.gt_status()
        det_status = self.det_status()
        pair_iou_by_gt = {g: v for g, _, v in self.match.pairs}
        gt_tiles = self.gt["tile_id"].astype(str) if len(self.gt) else pd.Series(dtype=str)
        det_tiles = self.det["tile_id"].astype(str) if len(self.det) else pd.Series(dtype=str)
        all_tiles = list(self.tile_ids)
        for extra in sorted(set(det_tiles.dropna()) - set(all_tiles)):
            all_tiles.append(extra)
        for tid in all_tiles:
            gt_mask = gt_tiles == tid
            det_mask = det_tiles == tid
            tp = int((gt_status[gt_mask] == "tp").sum()) if gt_mask.any() else 0
            fn = int((gt_status[gt_mask] == "fn").sum()) if gt_mask.any() else 0
            fp = int((det_status[det_mask] == "fp").sum()) if det_mask.any() else 0
            p, r, f1 = precision_recall_f1(tp, fp, fn)
            ious = [
                pair_iou_by_gt[i] for i in np.nonzero(gt_mask.to_numpy())[0] if i in pair_iou_by_gt
            ]
            rows.append(
                {
                    "tile_id": tid,
                    "stratum": self.strata.get(tid, "unknown"),
                    "n_gt": int(gt_mask.sum()),
                    "n_det": int(det_mask.sum()),
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "precision": p,
                    "recall": r,
                    "f1": f1,
                    "mean_iou_matched": mean_iou(ious),
                }
            )
        return pd.DataFrame(
            rows,
            columns=[
                "tile_id",
                "stratum",
                "n_gt",
                "n_det",
                "tp",
                "fp",
                "fn",
                "precision",
                "recall",
                "f1",
                "mean_iou_matched",
            ],
        )

    def per_stratum(self) -> dict[str, dict[str, Any]]:
        table = self.per_tile()
        out: dict[str, dict[str, Any]] = {}
        if table.empty:
            return out
        pair_iou_by_gt = {g: v for g, _, v in self.match.pairs}
        for stratum, grp in table.groupby("stratum", sort=True):
            tp, fp, fn = int(grp["tp"].sum()), int(grp["fp"].sum()), int(grp["fn"].sum())
            p, r, f1 = precision_recall_f1(tp, fp, fn)
            tile_set = set(grp["tile_id"])
            ious = [
                pair_iou_by_gt[i]
                for i, tid in enumerate(self.gt["tile_id"].astype(str))
                if tid in tile_set and i in pair_iou_by_gt
            ]
            out[str(stratum)] = {
                "n_tiles": int(len(grp)),
                "n_gt": int(grp["n_gt"].sum()),
                "n_det": int(grp["n_det"].sum()),
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "precision": p,
                "recall": r,
                "f1": f1,
                "mean_iou_matched": mean_iou(ious),
            }
        return out

    def to_report(self, generated_at: str, git_commit: str | None, smoke: bool) -> dict[str, Any]:
        """The payload of ``reports/metrics_<split>.json``."""
        return {
            "schema_version": "1.0",
            "split": self.split,
            "generated_at": generated_at,
            "git_commit": git_commit,
            "is_metric": not smoke,
            "labels_path": self.labels_path,
            "labels_verified": bool(self.labels_verified),
            "detections_path": self.detections_path,
            "model_version": self.model_version,
            "conf_threshold": self.conf_threshold,
            "iou_threshold": self.match.iou_threshold,
            "match_method": self.match.method,
            "metric_crs": self.metric_crs,
            "metric_crs_source": self.metric_crs_source,
            "n_tiles": len(self.tile_ids),
            "n_gt": int(len(self.gt)),
            "n_det": int(len(self.det)),
            "n_det_total": self.n_det_total,
            "detection": self.summary(),
            "per_stratum": self.per_stratum(),
            "per_tile": self.per_tile().to_dict(orient="records"),
            "notes": list(self.notes),
        }


def _assign_tiles(
    det: gpd.GeoDataFrame, tiles: gpd.GeoDataFrame | None, tile_ids: list[str]
) -> pd.Series:
    """Tile of each detection: the tile containing its representative point, else its own tile_id."""
    own = (
        det["tile_id"].astype("object").where(det["tile_id"].notna(), None)
        if "tile_id" in det.columns
        else pd.Series([None] * len(det), index=det.index, dtype=object)
    )
    own = own.map(lambda v: str(v) if v is not None and str(v) in tile_ids else None)
    if tiles is None or len(det) == 0:
        return own.astype(object)
    points = gpd.GeoDataFrame(geometry=det.geometry.representative_point(), crs=det.crs)
    joined = gpd.sjoin(points, tiles[["tile_id", "geometry"]], how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    spatial = joined["tile_id"].reindex(det.index)
    return spatial.where(spatial.notna(), own).astype(object)


def clip_to_tiles(
    det: gpd.GeoDataFrame, tiles: gpd.GeoDataFrame, min_area_m2: float
) -> gpd.GeoDataFrame:
    """Intersect detections with the union of tile polygons; drop fragments below ``min_area_m2``."""
    if len(det) == 0:
        return det.copy()
    union = tiles.geometry.union_all()
    tree = STRtree([union])
    hits = tree.query(det.geometry.to_numpy(), predicate="intersects")[0]
    clipped = det.iloc[np.unique(hits)].copy() if hits.size else det.iloc[0:0].copy()
    if len(clipped) == 0:
        return clipped
    clipped["geometry"] = clipped.geometry.intersection(union)
    clipped = clean_polygons(clipped)
    clipped = clipped[clipped.geometry.area >= float(min_area_m2)]
    return gpd.GeoDataFrame(clipped, geometry="geometry", crs=det.crs)


def evaluate_geodataframes(
    gt: gpd.GeoDataFrame,
    det: gpd.GeoDataFrame,
    tiles: gpd.GeoDataFrame | None,
    tile_ids: list[str],
    strata: dict[str, str],
    *,
    split: str = "val",
    metric_crs: str,
    metric_crs_source: str = "given",
    iou_threshold: float = 0.5,
    method: str = "greedy",
    min_area_m2: float = 12.0,
    notes: list[str] | None = None,
) -> SplitEvaluation:
    """Evaluate GT vs detections of one split. All frames must already be in ``metric_crs``.

    GT is restricted to rows whose ``tile_id`` is in ``tile_ids``. Detections are clipped to the
    union of tile polygons when ``tiles`` is given, else filtered by their own ``tile_id``.
    """
    notes = list(notes or [])
    gt = clean_polygons(gt)
    det = clean_polygons(det)
    if "tile_id" not in gt.columns:
        raise EvalInputError("labels lack the `tile_id` property (CONTRACTS §3.5)")
    gt = gt.copy()
    gt["tile_id"] = gt["tile_id"].astype(str)
    dropped = int((~gt["tile_id"].isin(tile_ids)).sum())
    if dropped:
        notes.append(f"{dropped} GT features outside the {split} tiles were ignored")
    gt = gt[gt["tile_id"].isin(tile_ids)].reset_index(drop=True)
    gt["stratum"] = gt["tile_id"].map(lambda t: strata.get(t, "unknown"))

    if tiles is not None and len(tiles) > 0:
        det = clip_to_tiles(det, tiles, min_area_m2).reset_index(drop=True)
    else:
        notes.append("tile polygons unavailable: detections filtered by their tile_id, not clipped")
        if "tile_id" in det.columns:
            det = det[det["tile_id"].astype(str).isin(tile_ids)].reset_index(drop=True)
        else:
            notes.append("detections lack tile_id: all detections were used")
            det = det.reset_index(drop=True)
    det = det.copy()
    det["tile_id"] = _assign_tiles(det, tiles, tile_ids)
    det["stratum"] = det["tile_id"].map(lambda t: strata.get(t, "unknown") if t else "unknown")
    if "score" not in det.columns:
        det["score"] = np.nan

    matrix = iou_matrix(list(gt.geometry), list(det.geometry))
    match = match_one_to_one(list(gt.geometry), list(det.geometry), iou_threshold, method, matrix)
    return SplitEvaluation(
        split=split,
        metric_crs=metric_crs,
        metric_crs_source=metric_crs_source,
        gt=gt,
        det=det,
        tiles=tiles,
        match=match,
        iou_matrix=matrix,
        tile_ids=list(tile_ids),
        strata=dict(strata),
        notes=notes,
    )


def load_split_tiles(
    paths: EvalPaths, split: str
) -> tuple[list[str], dict[str, str], gpd.GeoDataFrame | None]:
    """Tile ids, strata and (optional) polygons (EPSG:4326) of a split."""
    if split not in ("val", "test"):
        raise EvalInputError(f"split must be val or test, got {split!r}")
    ts = read_tiles_split(paths.tiles_split)
    sub = ts[ts["split"] == split]
    if sub.empty:
        raise EvalInputError(f"no tiles with split={split!r} in {paths.tiles_split}")
    tile_ids = sorted(sub["tile_id"].astype(str).tolist())
    strata = dict(zip(sub["tile_id"].astype(str), sub["stratum"].astype(str), strict=True))
    tiles = read_tiles_index(paths.tiles_index, paths.tiles_dir, tile_ids)
    return tile_ids, strata, tiles


def val_training_note(cfg: dict[str, Any], tile_ids: Sequence[str]) -> list[str]:
    """Flag val tiles exported as fine-tuning material: their metrics are optimistic."""
    out_dir = ((cfg.get("cv") or {}).get("dataset") or {}).get("out_dir", "data/interim/dataset")
    split_csv = Path(resolve_path(out_dir)) / "split.csv"
    if not split_csv.exists():
        return []
    df = pd.read_csv(split_csv, dtype={"tile_id": str})
    n = int(df.loc[df["subset"] == "train", "tile_id"].isin(set(tile_ids)).sum())
    if not n:
        return []
    return [
        f"{n} of {len(tile_ids)} val tiles are fine-tuning material (subset=train in "
        f"{split_csv.name}): val metrics are optimistic, test is the unbiased estimate"
    ]


def evaluate_split(
    split: str,
    paths: EvalPaths,
    cfg: dict[str, Any],
    *,
    labels_path: Path | None = None,
    detections_path: Path | None = None,
    min_score: float | None = None,
    method: str | None = None,
    iou_threshold: float | None = None,
) -> SplitEvaluation:
    """Load the inputs of CONTRACTS §3.5/§3.6 for ``split`` and evaluate them."""
    ecfg = load_eval_config(cfg)
    method = method or str(ecfg["match_method"])
    iou_thr = float(iou_threshold if iou_threshold is not None else ecfg["iou_threshold"])
    labels_path = Path(labels_path) if labels_path else paths.labels(split)
    detections_path = Path(detections_path) if detections_path else paths.detections

    tile_ids, strata, tiles4326 = load_split_tiles(paths, split)
    gt4326 = read_geojson(labels_path, f"{split} labels")
    det4326 = read_geojson(detections_path, "detections")
    notes: list[str] = val_training_note(cfg, tile_ids) if split == "val" else []

    sample = gt4326 if len(gt4326) else det4326
    metric_crs, crs_source = resolve_metric_crs(cfg, sample, paths.manifest)
    gt = gt4326.to_crs(metric_crs)
    det = det4326.to_crs(metric_crs)
    tiles = None
    if tiles4326 is not None:
        tiles = tiles4326.to_crs(metric_crs)
        tiles["stratum"] = tiles["tile_id"].map(lambda t: strata.get(t, "unknown"))
        missing_tiles = sorted(set(tile_ids) - set(tiles["tile_id"]))
        if missing_tiles:
            notes.append(
                f"tile polygons missing for {len(missing_tiles)} tiles: {missing_tiles[:5]}"
            )

    labels_verified = (
        bool(len(gt))
        and "verified" in gt.columns
        and bool(gt["verified"].fillna(False).astype(bool).all())
    )
    if "verified" not in gt.columns:
        notes.append("labels have no `verified` property")
    if "split" in gt.columns:
        wrong = int((gt["split"].astype(str) != split).sum())
        if wrong:
            notes.append(f"{wrong} label features carry split != {split!r}")

    n_det_total = int(len(det))
    conf_threshold = None
    if min_score is not None and "score" in det.columns:
        det = det[det["score"].astype(float) >= float(min_score)]
        conf_threshold = float(min_score)
        notes.append(f"detections filtered by score >= {min_score}")
    model_version = None
    if "model_version" in det.columns and len(det):
        versions = sorted({str(v) for v in det["model_version"].dropna().unique()})
        model_version = versions[0] if len(versions) == 1 else ",".join(versions)
        if len(versions) > 1:
            notes.append(f"detections carry {len(versions)} model_version values")
    if conf_threshold is None:
        cv_thr = (cfg.get("cv") or {}).get("conf_threshold")
        conf_threshold = float(cv_thr) if cv_thr is not None else None

    result = evaluate_geodataframes(
        gt,
        det,
        tiles,
        tile_ids,
        strata,
        split=split,
        metric_crs=metric_crs,
        metric_crs_source=crs_source,
        iou_threshold=iou_thr,
        method=method,
        min_area_m2=float(ecfg["min_area_m2"]),
        notes=notes,
    )
    result.labels_verified = labels_verified
    result.labels_path = rel(labels_path, paths.repo_root)
    result.detections_path = rel(detections_path, paths.repo_root)
    result.model_version = model_version
    result.conf_threshold = conf_threshold
    result.n_det_total = n_det_total
    return result
