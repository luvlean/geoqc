"""Confidence scoring of discrepancies (pure functions, no DB access).

The exact formulas are documented in ``src/geoqc/matching/README.md`` and reproduced in
``tests/matching/test_scoring.py`` on concrete numbers. Every feature is in [0, 1]; weights of a
class are normalised to sum 1, so the result is in [0, 1] before the AOI edge penalty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

CLASSES = ("missing_in_map", "missing_on_image", "geometry_mismatch")

# Feature names used by each class formula (documentation + validation of config).
CLASS_TERMS: dict[str, tuple[str, ...]] = {
    "missing_in_map": ("score", "area", "secondary"),
    "missing_on_image": ("area", "secondary", "context"),
    "geometry_mismatch": ("score", "iou", "area"),
}


@dataclass(frozen=True)
class Features:
    """Scoring inputs for one discrepancy (see ``geoqc_discrepancy_features`` in SQL)."""

    cls: str
    score: float | None = None  # detector confidence of the det (None for missing_on_image)
    area_m2: float = 0.0  # area of the subject geometry in the metric CRS
    iou: float | None = None  # pair IoU (mismatch) or max IoU with any partner (missing_*)
    secondary_coverage: float = 0.0  # area(subject ∩ secondary) / area(subject)
    has_secondary: bool = False  # any secondary source rows loaded at all
    aoi_edge: bool = False  # subject touches the AOI edge zone


def clip01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else float(x)


def area_norm(area_m2: float, min_area_m2: float, max_area_m2: float) -> float:
    """``clip(log(area / min_area) / log(max_area / min_area), 0, 1)``; 0 for non-positive area."""
    if min_area_m2 <= 0 or max_area_m2 <= min_area_m2:
        raise ValueError("require 0 < min_area_m2 < max_area_m2")
    if area_m2 is None or area_m2 <= 0:
        return 0.0
    return clip01(math.log(area_m2 / min_area_m2) / math.log(max_area_m2 / min_area_m2))


def normalize_weights(weights: dict[str, float], drop: tuple[str, ...] = ()) -> dict[str, float]:
    """Return weights scaled to sum 1, optionally dropping terms (e.g. ``secondary``)."""
    kept = {k: float(v) for k, v in weights.items() if k not in drop}
    if any(v < 0 for v in kept.values()):
        raise ValueError("weights must be non-negative")
    total = sum(kept.values())
    if total <= 0:
        raise ValueError("weights must not all be zero")
    return {k: v / total for k, v in kept.items()}


def _class_weights(
    cfg_weights: dict[str, dict[str, float]], cls: str, has_secondary: bool
) -> dict[str, float]:
    if cls not in CLASS_TERMS:
        raise ValueError(f"unknown discrepancy class: {cls}")
    w = dict(cfg_weights.get(cls, {}))
    missing = [t for t in CLASS_TERMS[cls] if t not in w]
    if missing:
        raise ValueError(f"weights.{cls} is missing terms {missing}")
    drop = () if has_secondary else ("secondary",)
    return normalize_weights({t: w[t] for t in CLASS_TERMS[cls]}, drop=drop)


def confidence_missing_in_map(
    score: float, a_norm: float, agree_secondary: float, w: dict[str, float]
) -> float:
    """``w_score·score + w_area·area_norm + w_sec·agree_secondary`` (weights already normalised)."""
    return clip01(
        w.get("score", 0.0) * clip01(score)
        + w.get("area", 0.0) * clip01(a_norm)
        + w.get("secondary", 0.0) * clip01(agree_secondary)
    )


def confidence_missing_on_image(
    a_norm: float, overlap_secondary: float, max_iou_any_det: float, w: dict[str, float]
) -> float:
    """``w_area·area_norm + w_sec·(1 − overlap_secondary) + w_ctx·(1 − max_iou_any_det)``."""
    return clip01(
        w.get("area", 0.0) * clip01(a_norm)
        + w.get("secondary", 0.0) * (1.0 - clip01(overlap_secondary))
        + w.get("context", 0.0) * (1.0 - clip01(max_iou_any_det))
    )


def confidence_geometry_mismatch(
    score: float, iou: float, a_norm: float, w: dict[str, float]
) -> float:
    """``w_score·score + w_iou·(1 − iou) + w_area·area_norm``."""
    return clip01(
        w.get("score", 0.0) * clip01(score)
        + w.get("iou", 0.0) * (1.0 - clip01(iou))
        + w.get("area", 0.0) * clip01(a_norm)
    )


@dataclass(frozen=True)
class Scored:
    area_norm: float
    confidence_raw: float  # before the AOI edge factor
    confidence: float


def score_features(f: Features, cfg: dict[str, Any]) -> Scored:
    """Apply the class formula with weights/thresholds from ``config/matching.yaml``."""
    a_norm = area_norm(f.area_m2, float(cfg["min_area_m2"]), float(cfg["max_area_m2"]))
    w = _class_weights(cfg["weights"], f.cls, f.has_secondary)
    if f.cls == "missing_in_map":
        raw = confidence_missing_in_map(f.score or 0.0, a_norm, f.secondary_coverage, w)
    elif f.cls == "missing_on_image":
        raw = confidence_missing_on_image(a_norm, f.secondary_coverage, f.iou or 0.0, w)
    else:
        raw = confidence_geometry_mismatch(f.score or 0.0, f.iou or 0.0, a_norm, w)
    conf = raw * float(cfg.get("aoi_edge_factor", 1.0)) if f.aoi_edge else raw
    return Scored(area_norm=a_norm, confidence_raw=raw, confidence=clip01(conf))
