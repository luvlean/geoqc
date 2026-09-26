"""export-dataset: детерминизм сплита, формат YOLO-seg, маски, запрет на тестовую разметку."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import box

from geoqc.cv.dataset import (
    FORBIDDEN_SPLIT,
    assert_not_forbidden,
    export_dataset,
    polygon_to_yolo_lines,
    rasterize_mask,
    read_holdout_tile_ids,
    split_tiles,
)


def test_split_is_deterministic_and_respects_fraction():
    """Один seed -> одинаковое разбиение; доля holdout ~20 %; другой seed -> другое разбиение."""
    ids = [f"{r}_{c}" for r in range(5) for c in range(4)]  # 20 tiles
    a = split_tiles(ids, holdout_fraction=0.2, seed=42)
    b = split_tiles(ids, holdout_fraction=0.2, seed=42)
    assert a == b
    assert sum(v == "holdout" for v in a.values()) == 4
    assert set(a) == set(ids)
    c = split_tiles(ids, holdout_fraction=0.2, seed=7)
    assert c != a
    assert split_tiles(list(reversed(ids)), holdout_fraction=0.2, seed=42) == a  # order-independent


def test_split_stratified_keeps_holdout_in_each_stratum():
    """Стратифицированный сплит: в каждой страте (>= 2 тайла) есть хотя бы один holdout."""
    ids = [f"0_{i}" for i in range(10)]
    strata = {tid: ("dense" if i < 5 else "industrial") for i, tid in enumerate(ids)}
    a = split_tiles(ids, holdout_fraction=0.2, seed=1, strata=strata)
    for s in ("dense", "industrial"):
        members = [t for t in ids if strata[t] == s]
        assert any(a[t] == "holdout" for t in members)
        assert any(a[t] == "train" for t in members)
    assert split_tiles(["only"], holdout_fraction=0.2, seed=1) == {"only": "train"}
    with pytest.raises(ValueError):
        split_tiles(ids, holdout_fraction=1.0)


def test_yolo_lines_are_normalised_and_masks_binary():
    """Полигон -> строка YOLO-seg 'class x y ...' в [0,1]; маска rasterize — только 0/255 с верной площадью."""
    t = from_origin(0.0, 64.0, 1.0, 1.0)  # 64 x 64 px, 1 m/px
    poly = box(10, 10, 30, 40)  # 20 x 30 m
    lines = polygon_to_yolo_lines(poly, t, 64, 64)
    assert len(lines) == 1
    parts = lines[0].split()
    assert parts[0] == "0" and len(parts) == 1 + 2 * 4
    vals = [float(v) for v in parts[1:]]
    assert all(0.0 <= v <= 1.0 for v in vals)
    xs, ys = vals[0::2], vals[1::2]
    assert min(xs) == pytest.approx(10 / 64) and max(xs) == pytest.approx(30 / 64)
    assert min(ys) == pytest.approx(24 / 64) and max(ys) == pytest.approx(54 / 64)
    mask = rasterize_mask([poly], t, (64, 64))
    assert set(np.unique(mask).tolist()) == {0, 255}
    assert int((mask == 255).sum()) == 20 * 30
    assert rasterize_mask([], t, (8, 8)).sum() == 0


def test_export_dataset_end_to_end(synthetic_aoi: dict):
    """Экспорт на синтетике: 2 val-тайла -> 1 train + 1 holdout, txt/png/маски, data.yaml, split.csv, meta.json."""
    out = synthetic_aoi["data"] / "interim" / "dataset"
    s = export_dataset(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        labels_path=synthetic_aoi["labels"],
        split_csv=synthetic_aoi["split_csv"],
        out_dir=out,
    )
    assert s.n_train == 1 and s.n_holdout == 1
    assert s.n_instances_train + s.n_instances_holdout == 4  # A counted in both tiles, B, C
    for sub in ("train", "holdout"):
        imgs = list((out / "images" / sub).glob("*.png"))
        assert len(imgs) == 1
        tid = imgs[0].stem
        assert (out / "labels" / sub / f"{tid}.txt").exists()
        assert (out / "masks" / sub / f"{tid}.png").exists()
    data_yaml = (out / "data.yaml").read_text()
    assert "images/train" in data_yaml and "images/holdout" in data_yaml and "building" in data_yaml
    meta = json.loads((out / "meta.json").read_text())
    assert meta["seed"] == 42 and meta["holdout_fraction"] == 0.5
    hold = read_holdout_tile_ids(out)
    assert len(hold) == 1 and hold[0] in {"0_0", "0_1"}
    # determinism: a second export yields the same assignment
    s2 = export_dataset(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        labels_path=synthetic_aoi["labels"],
        split_csv=synthetic_aoi["split_csv"],
        out_dir=out,
    )
    assert read_holdout_tile_ids(out) == hold and s2.n_holdout == 1


def test_export_requires_verified_by_default(synthetic_aoi: dict):
    """Черновик без verified=true даёт пустой датасет; --allow-unverified его принимает."""
    draft = synthetic_aoi["labels"].with_name("draft.geojson")
    fc = json.loads(synthetic_aoi["labels"].read_text())
    for f in fc["features"]:
        f["properties"]["verified"] = False
    draft.write_text(json.dumps(fc))
    out = synthetic_aoi["data"] / "interim" / "ds_draft"
    s = export_dataset(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        labels_path=draft,
        split_csv=synthetic_aoi["split_csv"],
        out_dir=out,
    )
    assert s.n_instances_train + s.n_instances_holdout == 0
    s = export_dataset(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        labels_path=draft,
        split_csv=synthetic_aoi["split_csv"],
        out_dir=out,
        require_verified=False,
    )
    assert s.n_instances_train + s.n_instances_holdout == 4


def test_forbidden_labels_path_is_rejected(tmp_path: Path):
    """Любой путь внутри каталога разметки тестового сплита отклоняется PermissionError."""
    bad = tmp_path / "data" / "labels" / FORBIDDEN_SPLIT / "buildings.geojson"
    with pytest.raises(PermissionError):
        assert_not_forbidden(bad)
    assert assert_not_forbidden(tmp_path / "data" / "labels" / "val" / "buildings.geojson")
