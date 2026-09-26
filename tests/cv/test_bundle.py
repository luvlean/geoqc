"""bundle: архив содержит тайлы, val-разметку, split csv и config, но не разметку тестового сплита."""

from __future__ import annotations

import zipfile

import pytest

from geoqc.cv.bundle import build_bundle, is_forbidden_arcname
from geoqc.cv.dataset import FORBIDDEN_SPLIT


def test_is_forbidden_arcname():
    """Детектор запрещённых путей ловит labels/<test> в любом месте пути и не трогает val."""
    assert is_forbidden_arcname(f"data/labels/{FORBIDDEN_SPLIT}/buildings.geojson")
    assert is_forbidden_arcname(f"x/LABELS/{FORBIDDEN_SPLIT.upper()}/a.geojson")
    assert not is_forbidden_arcname("data/labels/val/buildings.geojson")
    assert not is_forbidden_arcname("data/labels/tiles_split.csv")
    assert not is_forbidden_arcname(f"tests/{FORBIDDEN_SPLIT}_labels.py")


def test_bundle_contents(synthetic_aoi: dict):
    """Архив: 3 tif + gpkg + val geojson + tiles_split.csv + yaml/aoi + исходники geoqc; ни одного пути labels/<test>."""
    s = build_bundle(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        tiles_dir=synthetic_aoi["tiles_dir"],
        index_path=synthetic_aoi["index"],
        labels_dir=synthetic_aoi["labels_dir"],
        config_dir=synthetic_aoi["config_dir"],
        root=synthetic_aoi["root"],
    )
    with zipfile.ZipFile(s.path) as zf:
        names = zf.namelist()
    assert s.n_tiles == 3
    assert sum(n.endswith(".tif") for n in names) == 3
    assert "data/tiles/tiles.gpkg" in names
    assert "data/labels/val/buildings.geojson" in names
    assert "data/labels/tiles_split.csv" in names
    assert (
        "config/cv.yaml" in names
        and "config/pipeline.yaml" in names
        and "config/aoi.geojson" in names
    )
    assert "src/geoqc/cv/predictor.py" in names and "pyproject.toml" in names
    assert not any(is_forbidden_arcname(n) for n in names)
    assert not any(FORBIDDEN_SPLIT in n.split("/") for n in names)
    assert s.n_files == len(names) and s.size_bytes > 0


def test_bundle_val_tiles_only(synthetic_aoi: dict):
    """--tiles val: только 2 val-тайла, test-тайл (растр) не включается."""
    s = build_bundle(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        tiles="val",
        tiles_dir=synthetic_aoi["tiles_dir"],
        index_path=synthetic_aoi["index"],
        labels_dir=synthetic_aoi["labels_dir"],
        split_csv=synthetic_aoi["split_csv"],
        config_dir=synthetic_aoi["config_dir"],
        root=synthetic_aoi["root"],
    )
    with zipfile.ZipFile(s.path) as zf:
        tifs = sorted(n for n in zf.namelist() if n.endswith(".tif"))
    assert tifs == ["data/tiles/0_0.tif", "data/tiles/0_1.tif"]
    with pytest.raises(ValueError):
        build_bundle(
            synthetic_aoi["cv"],
            synthetic_aoi["pipeline"],
            tiles=FORBIDDEN_SPLIT,
            root=synthetic_aoi["root"],
        )


def test_bundle_includes_dataset_when_present(synthetic_aoi: dict):
    """Если export-dataset уже выполнен, его каталог попадает в архив (и исключается флагом)."""
    from geoqc.cv.dataset import export_dataset

    ds = synthetic_aoi["data"] / "interim" / "dataset"
    export_dataset(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        labels_path=synthetic_aoi["labels"],
        split_csv=synthetic_aoi["split_csv"],
        out_dir=ds,
    )
    kw = dict(
        tiles_dir=synthetic_aoi["tiles_dir"],
        index_path=synthetic_aoi["index"],
        labels_dir=synthetic_aoi["labels_dir"],
        config_dir=synthetic_aoi["config_dir"],
        dataset_dir=ds,
        root=synthetic_aoi["root"],
    )
    with_ds = build_bundle(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        out=synthetic_aoi["data"] / "interim" / "with_ds.zip",
        **kw,
    )
    without = build_bundle(
        synthetic_aoi["cv"], synthetic_aoi["pipeline"], include_dataset=False, **kw
    )
    assert with_ds.n_files > without.n_files
    with zipfile.ZipFile(with_ds.path) as zf:
        assert "data/interim/dataset/data.yaml" in zf.namelist()
