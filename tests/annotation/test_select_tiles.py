"""Tests for stratified tile selection (select_tiles.py)."""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from geoqc.annotation.cli import app
from geoqc.annotation.common import STRATA, TILES_SPLIT_COLUMNS, Paths
from geoqc.annotation.select_tiles import (
    DEFAULT_STRATA_CFG,
    _allocate,
    classify_tile,
    pick_recheck,
    sample_tiles,
    select_tiles,
    tag_group,
)
from tests.annotation.conftest import EMPTY_TILES, HALF


def _feat(**kw):
    base = {
        "n_buildings": 10,
        "built_share": 0.2,
        "mean_footprint_m2": 200.0,
        "share_dense_multistorey": 0.0,
        "share_low_rise_private": 0.0,
        "share_industrial": 0.0,
        "share_other": 0.0,
    }
    base.update(kw)
    return base


def test_tag_group_mapping():
    """Теги OSM building=* отображаются в группы страт; неизвестные и yes -> other."""
    tags = DEFAULT_STRATA_CFG["tags"]
    assert tag_group("apartments", tags) == "dense_multistorey"
    assert tag_group("House", tags) == "low_rise_private"
    assert tag_group("warehouse", tags) == "industrial"
    assert tag_group("yes", tags) == "other"
    assert tag_group("church", tags) == "other"


def test_classify_tile_rules():
    """Классификация тайла: доминирующая группа по площади, fallback по размеру футпринта, mixed, исключение пустых."""
    s = DEFAULT_STRATA_CFG
    assert classify_tile(_feat(share_industrial=0.7), s, 3) == "industrial"
    assert (
        classify_tile(_feat(share_dense_multistorey=0.5, share_low_rise_private=0.4), s, 3)
        == "dense_multistorey"
    )
    assert classify_tile(_feat(share_low_rise_private=0.45), s, 3) == "low_rise_private"
    assert (
        classify_tile(
            _feat(share_dense_multistorey=0.3, share_low_rise_private=0.3, share_other=0.4), s, 3
        )
        == "mixed"
    )
    # untagged (building=yes) tiles fall back to footprint statistics
    assert (
        classify_tile(_feat(share_other=1.0, mean_footprint_m2=600, built_share=0.3), s, 3)
        == "dense_multistorey"
    )
    assert classify_tile(_feat(share_other=1.0, mean_footprint_m2=90), s, 3) == "low_rise_private"
    assert classify_tile(_feat(share_other=1.0, mean_footprint_m2=250), s, 3) == "mixed"
    assert classify_tile(_feat(n_buildings=2), s, 3) is None


def test_allocate_respects_floor_and_total():
    """Распределение по стратам: сумма равна n_tiles, минимум на страту соблюдается, не больше кандидатов."""
    alloc = _allocate({"a": 30, "b": 2, "c": 10, "d": 6}, 24, 3)
    assert sum(alloc.values()) == 24
    assert alloc["b"] == 2  # only 2 candidates
    assert alloc["d"] >= 3 and alloc["c"] >= 3
    assert all(alloc[k] <= v for k, v in {"a": 30, "b": 2, "c": 10, "d": 6}.items())
    assert sum(_allocate({"a": 5, "b": 4}, 24, 3).values()) == 9  # capped by availability


def test_select_writes_contract_csv(selected: pd.DataFrame, paths: Paths):
    """tiles_split.csv имеет ровно колонки контракта, 24 тайла, все четыре страты, val и test не пересекаются."""
    assert list(selected.columns) == TILES_SPLIT_COLUMNS
    assert len(selected) == 24
    assert set(selected["split"]) == {"val", "test"}
    assert set(selected["stratum"]) == set(STRATA)
    val = set(selected.loc[selected["split"] == "val", "tile_id"])
    test = set(selected.loc[selected["split"] == "test", "tile_id"])
    assert not (val & test)
    assert selected["tile_id"].is_unique
    for stratum, grp in selected.groupby("stratum"):
        if len(grp) >= 2:
            assert set(grp["split"]) == {"val", "test"}, (
                f"stratum {stratum} must appear in both splits"
            )
    assert (paths.labels_dir / "select_tiles_report.json").exists()
    assert (paths.labels_dir / "tiles_features.csv").exists()


def test_strata_match_synthetic_quadrants(selected: pd.DataFrame):
    """Страты соответствуют квадрантам синтетического мира (apartments / house / industrial / mixed)."""
    for _, r in selected.iterrows():
        row, col = int(r["row"]), int(r["col"])
        expected = (
            "dense_multistorey"
            if row < HALF and col < HALF
            else "low_rise_private"
            if row < HALF
            else "industrial"
            if col < HALF
            else "mixed"
        )
        assert r["stratum"] == expected, (r["tile_id"], r["stratum"], expected)


def test_empty_tiles_excluded(selected: pd.DataFrame):
    """Тайлы без зданий OSM (меньше min_buildings_per_tile) не попадают в выборку."""
    assert not set(selected["tile_id"]) & {f"{r}_{c}" for r, c in EMPTY_TILES}


def test_no_adjacent_tiles(selected: pd.DataFrame):
    """При avoid_adjacent соседние тайлы (8-окрестность, перекрытие 64 px) не выбираются вместе."""
    cells = {(int(r), int(c)) for r, c in zip(selected["row"], selected["col"], strict=True)}
    for r, c in cells:
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if (dr, dc) != (0, 0):
                    assert (r + dr, c + dc) not in cells, (
                        f"tiles {(r, c)} and {(r + dr, c + dc)} are adjacent"
                    )


def test_selection_is_deterministic(cfg: dict, paths: Paths, synth_world: dict):
    """Один и тот же seed даёт побайтово одинаковый CSV; другой seed — другую выборку."""
    select_tiles(cfg, paths, synth_world["osm_path"], seed=42)
    first = paths.tiles_split_csv.read_text()
    first_recheck = paths.recheck_tiles_csv.read_text()
    select_tiles(cfg, paths, synth_world["osm_path"], seed=42)
    assert paths.tiles_split_csv.read_text() == first
    assert paths.recheck_tiles_csv.read_text() == first_recheck
    select_tiles(cfg, paths, synth_world["osm_path"], seed=7)
    assert paths.tiles_split_csv.read_text() != first


def test_recheck_subset(selected: pd.DataFrame, paths: Paths, cfg: dict):
    """Recheck-тайлы — подмножество выбранных, размер max(recheck_min_tiles, ceil(n·recheck_fraction)), охватывает оба сплита."""
    rc = pd.read_csv(paths.recheck_tiles_csv, dtype=str)
    n = len(selected)
    expected = max(
        int(cfg["annotation"]["recheck_min_tiles"]),
        math.ceil(n * cfg["annotation"]["recheck_fraction"]),
    )
    assert len(rc) == expected
    assert set(rc["tile_id"]) <= set(selected["tile_id"])
    assert set(rc["split"]) == {"val", "test"}


def test_sample_tiles_handles_small_pool():
    """Если кандидатов меньше n_tiles, берутся все; одиночная страта не ломает сплит."""
    feats = pd.DataFrame(
        {
            "tile_id": ["0_0", "0_2", "2_0", "4_4", "6_6"],
            "row": [0, 0, 2, 4, 6],
            "col": [0, 2, 0, 4, 6],
            "stratum": ["mixed", "mixed", "mixed", "industrial", None],
        }
    )
    out = sample_tiles(feats, n_tiles=24, test_fraction=0.5, seed=1)
    assert len(out) == 4
    assert set(out["tile_id"]) == {"0_0", "0_2", "2_0", "4_4"}
    rc = pick_recheck(out, 0.1, 2, 1)
    assert len(rc) == 2


def test_cli_select_tiles(cli_env: Path, paths: Paths, synth_world: dict):
    """CLI select-tiles пишет tiles_split.csv с переопределёнными путями."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "select-tiles",
            "--osm",
            str(synth_world["osm_path"]),
            "--labels-dir",
            str(paths.labels_dir),
            "--tiles-index",
            str(paths.tiles_index),
            "--n-tiles",
            "20",
        ],
    )
    assert res.exit_code == 0, res.output
    df = pd.read_csv(paths.tiles_split_csv)
    assert len(df) == 20
    assert "recheck tiles" in res.output


@pytest.mark.parametrize("missing", ["osm"])
def test_cli_select_tiles_missing_osm(cli_env: Path, paths: Paths, tmp_path: Path, missing: str):
    """Без файла OSM команда завершается кодом 2 и понятным сообщением."""
    res = CliRunner().invoke(
        app,
        [
            "select-tiles",
            "--osm",
            str(tmp_path / "nope.geojson"),
            "--labels-dir",
            str(paths.labels_dir),
            "--tiles-index",
            str(paths.tiles_index),
        ],
    )
    assert res.exit_code == 2
