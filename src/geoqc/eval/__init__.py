"""Evaluation module: detection metrics, verification of discrepancies, quality gates."""

from geoqc.eval.detection_metrics import (
    MatchResult,
    iou,
    match_one_to_one,
    mean_iou,
    precision_recall_f1,
)

__all__ = ["MatchResult", "iou", "match_one_to_one", "mean_iou", "precision_recall_f1"]
