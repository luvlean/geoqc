"""Area-weighted F1 (D-023) of candidate models on val over a threshold grid -> reports/experiments/weighted_f1_val.json."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

REPO = Path.cwd()
MODELS = {
    "base_512": "data/interim/experiments/det_val_base_512.geojson",
    "base_768": "data/interim/experiments/det_val_base_768.geojson",
    "full_default": "data/interim/experiments/det_val_full_default.geojson",
    "full_aerial": "data/interim/experiments/det_val_full_aerial.geojson",
    "head_aerial": "data/interim/experiments/det_val_head_aerial.geojson",
    "head_aerial_768": "data/interim/experiments/det_val_head_aerial_768.geojson",
    "full_aerial_768": "data/interim/experiments/det_val_full_aerial_768.geojson",
    "airs_768": "data/interim/experiments/det_val_airs_768.geojson",
    "airs_rid2_masked_768": "data/interim/experiments/det_val_airs_rid2_masked.geojson",
    "airs_rid2_full_768": "data/interim/experiments/det_val_airs_rid2_full.geojson",
}
GRID = [0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7]
KEYS = (
    "precision",
    "recall",
    "f1",
    "precision_area",
    "recall_area",
    "f1_area",
    "mean_iou_matched",
    "tp",
    "fp",
    "fn",
)

out = {}
with tempfile.TemporaryDirectory() as tmp:
    for name, det in MODELS.items():
        if not (REPO / det).exists():
            continue
        rows = []
        for t in GRID:
            f = Path(tmp) / f"{name}_{t}.json"
            subprocess.run(
                [
                    str(REPO / ".venv/bin/geoqc"),
                    "eval",
                    "detection",
                    "--split",
                    "val",
                    "--detections",
                    det,
                    "--min-score",
                    str(t),
                    "--out",
                    str(f),
                ],
                check=True,
                capture_output=True,
            )
            d = json.loads(f.read_text())["detection"]
            rows.append({"conf": t, **{k: d[k] for k in KEYS}})
        best = max(rows, key=lambda r: (r["f1_area"], r["precision"], r["conf"]))
        out[name] = {"detections": det, "best_by_f1_area": best, "thresholds": rows}
        print(
            f"{name:22s} best conf={best['conf']:.2f} F1area={best['f1_area']:.3f} F1={best['f1']:.3f} "
            f"Parea={best['precision_area']:.3f} Rarea={best['recall_area']:.3f} IoU={best['mean_iou_matched']:.3f}"
        )
Path("reports/experiments/weighted_f1_val.json").write_text(json.dumps(out, indent=2))
