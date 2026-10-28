"""Fine-tune the train+AIRS model on hard RID2 small-building chips; two variants, scored on our val.

Start: models/yolov8m-building-seg-ft-ours-airs.pt. Train list per variant: our train x REPEAT +
N_AIRS random AIRS chips (replay, so large roofs are not forgotten) + RID2 chips of the variant.
Early stopping on our val (holdout of data/interim/dataset), then F1 per threshold on val as in
airs_night.py. Results: data/interim/external/rid2/result_<variant>.json; config is not touched.
Usage: RID2_DIR=... python scripts/experiments/rid2_train.py
"""

from __future__ import annotations

import copy
import json
import random
import subprocess
import time
from pathlib import Path

REPO = Path.cwd()
OUT = REPO / "data/interim/external/rid2"
OURS = REPO / "data/interim/dataset"
AIRS = REPO / "data/interim/external/airs/images/train"
START = REPO / "models/yolov8m-building-seg-ft-ours-airs.pt"
BASELINE_F1 = 0.8615  # train + AIRS at 768 on val (night_result.json)
REPEAT, N_AIRS, SEED = 10, 400, 42
LOG = OUT / "train.log"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def train(variant: str) -> Path:
    from ultralytics import YOLO

    run = f"finetune_yolov8m_seg_airs_rid2_{variant}_768"
    best = REPO / "models/runs" / run / "weights/best.pt"
    if (REPO / "models/runs" / run / ".done").exists():
        return best
    ours = sorted(str(p) for p in (OURS / "images/train").glob("*.png"))
    airs = sorted(str(p) for p in AIRS.glob("*.png"))
    airs = random.Random(SEED).sample(airs, min(N_AIRS, len(airs)))
    rid2 = sorted(str(p) for p in (OUT / variant / "images/train").glob("*.png"))
    lst = OUT / f"train_list_{variant}.txt"
    lst.write_text("\n".join(ours * REPEAT + airs + rid2) + "\n")
    yaml = OUT / f"data_{variant}.yaml"
    yaml.write_text(
        f"path: {OUT}\ntrain: {lst}\nval: {OURS / 'images/holdout'}\nnames:\n  0: building\nnc: 1\n"
    )
    log(
        f"[{variant}] train: ours {len(ours)} x {REPEAT} + airs {len(airs)} + rid2 {len(rid2)}; start {START.name}"
    )
    YOLO(str(START)).train(
        data=str(yaml),
        epochs=30,
        patience=8,
        time=4.0,
        imgsz=768,
        batch=4,
        device="mps",
        amp=True,
        optimizer="AdamW",
        lr0=1e-4,
        warmup_bias_lr=0.0,
        workers=0,
        cache=False,
        seed=SEED,
        deterministic=True,
        project=str(REPO / "models/runs"),
        name=run,
        exist_ok=True,
        plots=False,
        verbose=False,
        fliplr=0.5,
        flipud=0.5,
        degrees=90.0,
        hsv_s=0.5,
        hsv_v=0.3,
    )
    (REPO / "models/runs" / run / ".done").write_text("ok")
    log(f"[{variant}] trained -> {best}")
    return best


def score(variant: str, best: Path) -> None:
    from geoqc.config import load_config
    from geoqc.cv.pipeline import run_predict, write_detections

    cfg = load_config()
    cv = copy.deepcopy(cfg["cv"])
    cv["weights"] = {**cv["weights"], "path": str(best)}
    cv["imgsz"], cv["batch"] = 768, 4
    det = write_detections(
        run_predict(
            cv, cfg, split="val", conf=0.05, model_version=f"airs_rid2_{variant}"
        ).detections,
        OUT / f"det_val_airs_rid2_{variant}.geojson",
    )
    rows = []
    for t in (0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5):
        out = OUT / f"metrics_val_{variant}_{t}.json"
        subprocess.run(
            [
                str(REPO / ".venv/bin/geoqc"),
                "eval",
                "detection",
                "--split",
                "val",
                "--detections",
                str(det),
                "--min-score",
                str(t),
                "--out",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        d = json.loads(out.read_text())
        d = d.get("detection", d)
        rows.append(
            {
                "conf": t,
                **{
                    k: d[k]
                    for k in ("precision", "recall", "f1", "mean_iou_matched", "tp", "fp", "fn")
                },
            }
        )
    b = max(rows, key=lambda r: r["f1"])
    res = {
        "variant": variant,
        "weights": str(best),
        "baseline_airs_f1": BASELINE_F1,
        "best": b,
        "thresholds": rows,
    }
    (OUT / f"result_{variant}.json").write_text(json.dumps(res, indent=2))
    log(
        f"[{variant}] RESULT val F1={b['f1']:.3f} at conf={b['conf']} (train+AIRS {BASELINE_F1:.3f}): {b}"
    )


if __name__ == "__main__":
    try:
        if not (OUT / "prepared.json").exists():
            subprocess.run(
                [str(REPO / ".venv/bin/python"), "scripts/experiments/rid2_prepare.py"], check=True
            )
        for v in ("masked", "full"):
            score(v, train(v))
        log("rid2 run finished")
    except Exception as exc:
        log(f"FAILED: {type(exc).__name__}: {exc}")
        raise
