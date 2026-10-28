"""Night run: AIRS (Christchurch, 7.5 cm, roof masks) + our train labels -> fine-tune -> score on val.

Steps (each skipped when its output exists, so a crash can be resumed):
  0. wait for ~/Downloads/archive.zip (Safari renames it when the download is complete), unzip into
     data/raw/external/airs (its own directory, nothing from the archive is executed);
  1. image/label pairs -> 0.3 m (factor 4), 512 px chips, connected components -> YOLO-seg polygons
     (min 12 m2), deterministic sample of N chips -> data/interim/external/airs;
  2. train: our train tiles x REPEAT + AIRS chips, Ultralytics val = our val (holdout), imgsz 768;
  3. predict val at 768 with best.pt, F1 per threshold vs the frozen base model at 768 (0.792).
config/cv.yaml and the review queue are never touched. test labels are never read.
"""

from __future__ import annotations

import copy
import json
import os
import random
import subprocess
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np
import rasterio
from rasterio.enums import Resampling

REPO = (
    Path(__file__).resolve().parent
    if (Path(__file__).parent / "pyproject.toml").exists()
    else Path.cwd()
)
ZIP = Path.home() / "Downloads" / "archive.zip"
# Safari may unpack the archive itself (~/Downloads/archive-2) and delete the zip: use that folder.
RAW = Path(os.environ.get("AIRS_DIR", REPO / "data/raw/external/airs"))
OUT = REPO / "data/interim/external/airs"
OURS = REPO / "data/interim/dataset"  # D-021 export: images/train = train, images/holdout = val
RUN = "finetune_yolov8m_seg_ours_airs_768"
N_CHIPS, CHIP, FACTOR, MIN_PX, REPEAT, SEED = 1500, 512, 4, 134, 10, 42  # 134 px * 0.09 m2 = 12 m2
LOG = OUT / "night.log"


def log(msg: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def wait_and_unzip() -> None:
    if (RAW / ".unzipped").exists() or (RAW / "train" / "image").is_dir():
        return
    log(f"waiting for {ZIP}")
    last = -1
    while True:
        if ZIP.exists():
            size = ZIP.stat().st_size
            if size == last:
                break
            last = size
        time.sleep(60)
    log(f"unzipping {ZIP} ({last / 1e9:.1f} GB) -> {RAW}")
    RAW.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP) as zf:
        for m in zf.infolist():
            target = (RAW / m.filename).resolve()
            if not str(target).startswith(str(RAW.resolve())):  # zip-slip guard
                raise RuntimeError(f"unsafe path in archive: {m.filename}")
        zf.extractall(RAW)
    (RAW / ".unzipped").write_text("ok")
    log("unzipped")


def find_pairs() -> list[tuple[Path, Path]]:
    """(image, label) pairs from any '<split>/image*' + '<split>/label*' folders of the AIRS train part."""
    pairs = []
    for img_dir in sorted(
        p for p in RAW.rglob("*") if p.is_dir() and p.name.lower().startswith("image")
    ):
        if "test" in [s.lower() for s in img_dir.parts] or "val" in [
            s.lower() for s in img_dir.parts
        ]:
            continue  # keep the AIRS train part only
        lab_dirs = [
            d for d in img_dir.parent.iterdir() if d.is_dir() and d.name.lower().startswith("label")
        ]
        if not lab_dirs:
            continue
        labels = {
            p.stem: p
            for p in lab_dirs[0].iterdir()
            if p.suffix.lower() in (".tif", ".tiff", ".png")
        }
        for img in sorted(img_dir.iterdir()):
            if img.suffix.lower() in (".tif", ".tiff", ".png") and img.stem in labels:
                pairs.append((img, labels[img.stem]))
    return pairs


def read_down(path: Path, resampling: Resampling) -> np.ndarray:
    with rasterio.open(path) as src:
        h, w = src.height // FACTOR, src.width // FACTOR
        return src.read(out_shape=(src.count, h, w), resampling=resampling)


def polygons(mask: np.ndarray) -> list[np.ndarray]:
    n, cc, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    out = []
    for k in range(1, n):
        if stats[k, cv2.CC_STAT_AREA] < MIN_PX:
            continue
        cnts, _ = cv2.findContours(
            (cc == k).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        c = max(cnts, key=cv2.contourArea)
        c = cv2.approxPolyDP(c, 1.0, True).reshape(-1, 2)
        if len(c) >= 3:
            out.append(c)
    return out


def prepare() -> None:
    if (OUT / "prepared.json").exists():
        return
    pairs = find_pairs()
    log(f"AIRS train pairs: {len(pairs)}")
    if not pairs:
        raise RuntimeError(f"no image/label pairs under {RAW}; check the archive layout")
    rng = random.Random(SEED)
    rng.shuffle(pairs)
    (OUT / "images" / "train").mkdir(parents=True, exist_ok=True)
    (OUT / "labels" / "train").mkdir(parents=True, exist_ok=True)
    n_chips = n_inst = 0
    per_tile = max(2, N_CHIPS // max(1, len(pairs)) + 1)
    for img_p, lab_p in pairs:
        img = read_down(img_p, Resampling.average)[:3]
        lab = read_down(lab_p, Resampling.average)[0]
        img = np.transpose(img, (1, 2, 0)).astype(np.uint8)
        mask = lab > (lab.max() / 2 if lab.max() > 0 else 1)
        cells = [
            (r, c)
            for r in range(0, img.shape[0] - CHIP + 1, CHIP)
            for c in range(0, img.shape[1] - CHIP + 1, CHIP)
        ]
        rng.shuffle(cells)
        taken = 0
        for r, c in cells:
            chip, m = img[r : r + CHIP, c : c + CHIP], mask[r : r + CHIP, c : c + CHIP]
            if np.mean(np.any(chip > 0, axis=2)) < 0.9:
                continue
            polys = polygons(m)
            if not polys:
                continue
            name = f"{img_p.stem}_{r}_{c}"
            cv2.imwrite(str(OUT / "images" / "train" / f"{name}.png"), chip[:, :, ::-1])
            lines = ["0 " + " ".join(f"{x / CHIP:.6f} {y / CHIP:.6f}" for x, y in p) for p in polys]
            (OUT / "labels" / "train" / f"{name}.txt").write_text("\n".join(lines) + "\n")
            n_chips += 1
            n_inst += len(lines)
            taken += 1
            if taken >= per_tile or n_chips >= N_CHIPS:
                break
        if n_chips >= N_CHIPS:
            break
    meta = {
        "chips": n_chips,
        "instances": n_inst,
        "gsd_m": 0.075 * FACTOR,
        "source": "AIRS (Chen et al. 2019), LINZ CC BY 4.0",
    }
    (OUT / "prepared.json").write_text(json.dumps(meta, indent=2))
    log(f"prepared {meta}")


def train() -> Path:
    from ultralytics import YOLO

    best = REPO / "models" / "runs" / RUN / "weights" / "best.pt"
    if best.exists() and (REPO / "models" / "runs" / RUN / ".done").exists():
        return best
    ours = sorted(str(p) for p in (OURS / "images" / "train").glob("*.png"))
    airs = sorted(str(p) for p in (OUT / "images" / "train").glob("*.png"))
    lst = OUT / "train_list.txt"
    lst.write_text("\n".join(ours * REPEAT + airs) + "\n")
    yaml = OUT / "data.yaml"
    yaml.write_text(
        f"path: {OUT}\ntrain: {lst}\nval: {OURS / 'images' / 'holdout'}\nnames:\n  0: building\nnc: 1\n"
    )
    log(f"train: ours {len(ours)} x {REPEAT} + airs {len(airs)}; val = our val holdout")
    YOLO(str(REPO / "models" / "yolov8m-building-seg.pt")).train(
        data=str(yaml),
        epochs=40,
        patience=10,
        time=6.0,
        imgsz=768,
        batch=4,
        device="mps",
        amp=True,
        optimizer="AdamW",
        lr0=2e-4,
        warmup_bias_lr=0.0,
        workers=0,
        cache=False,
        seed=SEED,
        deterministic=True,
        project=str(REPO / "models" / "runs"),
        name=RUN,
        exist_ok=True,
        plots=False,
        verbose=False,
        fliplr=0.5,
        flipud=0.5,
        degrees=90.0,
        hsv_s=0.5,
        hsv_v=0.3,
    )
    (REPO / "models" / "runs" / RUN / ".done").write_text("ok")
    log(f"trained -> {best}")
    return best


def score(best: Path) -> None:
    from geoqc.config import load_config
    from geoqc.cv.pipeline import run_predict, write_detections

    cfg = load_config()
    cv = copy.deepcopy(cfg["cv"])
    cv["weights"] = {**cv["weights"], "path": str(best)}
    cv["imgsz"], cv["batch"] = 768, 4
    det = write_detections(
        run_predict(cv, cfg, split="val", conf=0.05, model_version=RUN).detections,
        OUT / "det_val_ours_airs_768.geojson",
    )
    rows = []
    for t in (0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5):
        out = OUT / f"metrics_val_{t}.json"
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
    best_row = max(rows, key=lambda r: r["f1"])
    res = {
        "model": RUN,
        "weights": str(best),
        "baseline_base_768_f1": 0.7925,
        "best": best_row,
        "thresholds": rows,
    }
    (OUT / "night_result.json").write_text(json.dumps(res, indent=2))
    log(
        f"RESULT val F1={best_row['f1']:.3f} at conf={best_row['conf']} (base@768 0.792): {best_row}"
    )


if __name__ == "__main__":
    try:
        wait_and_unzip()
        prepare()
        score(train())
        log("night run finished")
    except Exception as exc:  # leave the reason in the log for the morning
        log(f"FAILED: {type(exc).__name__}: {exc}")
        raise
