"""Stratified selection of annotation tiles and the val/test split (CONTRACTS §3.5).

Strata (``dense_multistorey``, ``low_rise_private``, ``industrial``, ``mixed``) are derived per tile
from OSM buildings: share of building *area* carrying the tags of each group, built-up density and
mean footprint size (fallback for untagged ``building=yes``). OSM is used only to stratify —
never as ground truth (DECISIONS D-005).

Outputs: ``data/labels/tiles_split.csv`` (``tile_id,split,stratum,row,col``),
``data/labels/recheck_tiles.csv`` (blind re-annotation subset for G2) and
``data/labels/select_tiles_report.json`` (diagnostics: candidates, exclusions, thresholds).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from geoqc.annotation.common import (
    STRATA,
    TILES_SPLIT_COLUMNS,
    Paths,
    building_tag,
    metric_crs,
    read_tiles,
    read_vector,
    stable_json,
)

TAG_GROUPS = ("dense_multistorey", "low_rise_private", "industrial")
UNTAGGED = "other"

DEFAULT_STRATA_CFG: dict[str, Any] = {
    "dominant_share": 0.4,
    "dense_density": 0.15,
    "large_footprint_m2": 400.0,
    "small_footprint_m2": 150.0,
    "tags": {
        "dense_multistorey": ["apartments", "dormitory"],
        "low_rise_private": ["house", "detached", "semidetached_house", "terrace", "bungalow"],
        "industrial": ["industrial", "warehouse", "factory", "manufacture", "hangar"],
    },
}


def tag_group(tag: str, tags_cfg: dict[str, list[str]]) -> str:
    """Map an OSM ``building=*`` value to a stratum tag group or ``other``."""
    t = tag.strip().lower()
    for group in TAG_GROUPS:
        if t in {v.lower() for v in tags_cfg.get(group, [])}:
            return group
    return UNTAGGED


def classify_tile(
    feat: dict[str, float], strata_cfg: dict[str, Any], min_buildings: int
) -> str | None:
    """Assign a stratum from tile features; ``None`` marks a tile that is not a candidate.

    Rule: the tag group with the largest share of building area wins if that share is at least
    ``dominant_share``; otherwise untagged tiles fall back to footprint statistics
    (large footprints + dense -> dense_multistorey, small footprints -> low_rise_private);
    everything else is ``mixed``.
    """
    if feat["n_buildings"] < min_buildings:
        return None
    shares = {g: feat[f"share_{g}"] for g in TAG_GROUPS}
    best = max(shares, key=lambda g: (shares[g], g))
    if shares[best] >= strata_cfg["dominant_share"]:
        return best
    if feat["share_other"] >= strata_cfg["dominant_share"]:
        if (
            feat["mean_footprint_m2"] >= strata_cfg["large_footprint_m2"]
            and feat["built_share"] >= strata_cfg["dense_density"]
        ):
            return "dense_multistorey"
        if feat["mean_footprint_m2"] < strata_cfg["small_footprint_m2"]:
            return "low_rise_private"
    return "mixed"


def tile_features(
    tiles: gpd.GeoDataFrame,
    osm: gpd.GeoDataFrame,
    crs_metric: str,
    strata_cfg: dict[str, Any],
    aoi: gpd.GeoDataFrame | None = None,
) -> pd.DataFrame:
    """Per-tile features used for stratification (areas in ``crs_metric``)."""
    tiles_m = tiles.to_crs(crs_metric).reset_index(drop=True)
    osm_m = osm.to_crs(crs_metric).reset_index(drop=True)
    osm_m = osm_m[osm_m.geometry.notna() & ~osm_m.geometry.is_empty]
    osm_m = osm_m[osm_m.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].reset_index(drop=True)
    osm_m["geometry"] = shapely.make_valid(osm_m.geometry.values)
    tags_cfg = strata_cfg.get("tags", DEFAULT_STRATA_CFG["tags"])
    groups = np.array(
        [tag_group(building_tag(r), tags_cfg) for _, r in osm_m.iterrows()], dtype=object
    )

    n_tiles = len(tiles_m)
    tile_area = tiles_m.geometry.area.to_numpy()
    n_b = np.zeros(n_tiles, dtype=int)
    area_by_group = {g: np.zeros(n_tiles) for g in (*TAG_GROUPS, UNTAGGED)}
    footprint_sum = np.zeros(n_tiles)

    if len(osm_m) > 0:
        t_idx, o_idx = osm_m.sindex.query(tiles_m.geometry, predicate="intersects")
        if len(t_idx) > 0:
            inter = shapely.intersection(
                tiles_m.geometry.values[t_idx], osm_m.geometry.values[o_idx]
            )
            inter_area = shapely.area(inter)
            full_area = shapely.area(osm_m.geometry.values[o_idx])
            keep = inter_area > 0
            t_idx, o_idx, inter_area, full_area = (
                t_idx[keep],
                o_idx[keep],
                inter_area[keep],
                full_area[keep],
            )
            np.add.at(n_b, t_idx, 1)
            np.add.at(footprint_sum, t_idx, full_area)
            for g in area_by_group:
                mask = groups[o_idx] == g
                np.add.at(area_by_group[g], t_idx[mask], inter_area[mask])

    total = sum(area_by_group.values())
    with np.errstate(divide="ignore", invalid="ignore"):
        shares = {g: np.where(total > 0, a / total, 0.0) for g, a in area_by_group.items()}
        mean_fp = np.where(n_b > 0, footprint_sum / np.maximum(n_b, 1), 0.0)

    feats = pd.DataFrame(
        {
            "tile_id": tiles_m["tile_id"].to_numpy(),
            "row": tiles_m["row"].to_numpy(),
            "col": tiles_m["col"].to_numpy(),
            "n_buildings": n_b,
            "built_share": total / tile_area,
            "mean_footprint_m2": mean_fp,
            **{f"share_{g}": shares[g] for g in (*TAG_GROUPS, UNTAGGED)},
        }
    )
    if aoi is not None and len(aoi) > 0:
        aoi_geom = aoi.to_crs(crs_metric).union_all()
        feats["aoi_coverage"] = (
            shapely.area(shapely.intersection(tiles_m.geometry.values, aoi_geom)) / tile_area
        )
    else:
        feats["aoi_coverage"] = 1.0
    return feats


def _allocate(candidates: dict[str, int], n_tiles: int, min_per_stratum: int) -> dict[str, int]:
    """Proportional allocation with a per-stratum floor, capped by availability."""
    total = sum(candidates.values())
    if total == 0:
        return dict.fromkeys(candidates, 0)
    n_tiles = min(n_tiles, total)
    alloc = {}
    for s, c in candidates.items():
        floor = min(min_per_stratum, c)
        alloc[s] = min(c, max(floor, int(round(n_tiles * c / total))))
    strata_sorted = sorted(candidates)  # deterministic tie-breaking
    while sum(alloc.values()) > n_tiles:
        reducible = [s for s in strata_sorted if alloc[s] > min(min_per_stratum, candidates[s])]
        if not reducible:
            reducible = [s for s in strata_sorted if alloc[s] > 0]
        s = max(reducible, key=lambda k: (alloc[k], k))
        alloc[s] -= 1
    while sum(alloc.values()) < n_tiles:
        expandable = [s for s in strata_sorted if alloc[s] < candidates[s]]
        if not expandable:
            break
        s = max(expandable, key=lambda k: (candidates[k] - alloc[k], k))
        alloc[s] += 1
    return alloc


def _is_adjacent(a: tuple[int, int], selected: set[tuple[int, int]]) -> bool:
    r, c = a
    return any(
        (r + dr, c + dc) in selected for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0)
    )


def sample_tiles(
    feats: pd.DataFrame,
    n_tiles: int,
    test_fraction: float,
    seed: int,
    min_per_stratum: int = 3,
    avoid_adjacent: bool = True,
) -> pd.DataFrame:
    """Stratified sampling + val/test split. ``feats`` must have a ``stratum`` column (None = excluded)."""
    rng = np.random.default_rng(seed)
    cand = feats[feats["stratum"].notna()].copy()
    cand = cand.sort_values(["stratum", "row", "col"]).reset_index(drop=True)
    candidates = {
        s: int((cand["stratum"] == s).sum()) for s in STRATA if (cand["stratum"] == s).any()
    }
    alloc = _allocate(candidates, n_tiles, min_per_stratum)

    selected: list[dict[str, Any]] = []
    occupied: set[tuple[int, int]] = set()
    leftovers: list[dict[str, Any]] = []
    for s in sorted(candidates):
        rows = cand[cand["stratum"] == s].to_dict("records")
        order = rng.permutation(len(rows))
        taken = 0
        for i in order:
            r = rows[i]
            rc = (int(r["row"]), int(r["col"]))
            if taken >= alloc[s]:
                leftovers.append(r)
                continue
            if avoid_adjacent and _is_adjacent(rc, occupied):
                leftovers.append(r)
                continue
            selected.append(r)
            occupied.add(rc)
            taken += 1
    # second pass: fill remaining slots from leftovers of any stratum (adjacency may have blocked some)
    target = min(n_tiles, len(cand))
    if len(selected) < target and leftovers:
        for i in rng.permutation(len(leftovers)):
            if len(selected) >= target:
                break
            r = leftovers[i]
            rc = (int(r["row"]), int(r["col"]))
            if rc in occupied or (avoid_adjacent and _is_adjacent(rc, occupied)):
                continue
            selected.append(r)
            occupied.add(rc)

    out = pd.DataFrame(selected)
    if out.empty:
        return pd.DataFrame(columns=TILES_SPLIT_COLUMNS)
    out["split"] = "val"
    for s in sorted(out["stratum"].unique()):
        idx = out.index[out["stratum"] == s].to_numpy()
        k = len(idx)
        n_test = int(round(k * test_fraction))
        if k >= 2:
            n_test = min(max(n_test, 1), k - 1)
        elif k == 1:
            n_test = int(rng.random() < test_fraction)
        perm = rng.permutation(k)
        out.loc[idx[perm[:n_test]], "split"] = "test"
    out = out[TILES_SPLIT_COLUMNS].astype({"row": int, "col": int})
    return out.sort_values(["split", "stratum", "row", "col"]).reset_index(drop=True)


def add_test_tiles(
    tiles_split: pd.DataFrame,
    feats: pd.DataFrame,
    n_tiles: int,
    seed: int,
    min_per_stratum: int = 2,
    min_aoi_coverage: float = 0.9,
) -> pd.DataFrame:
    """New ``test`` rows: stratified, not adjacent to any tile already in ``tiles_split`` or to each other.

    ``feats`` is ``tiles_features.csv`` written by ``select-tiles`` (stratum per tile). Used once for
    D-021, when the old val/test became train/val and a fresh test set was drawn.
    """
    rng = np.random.default_rng(seed)
    occupied = {
        (int(r), int(c)) for r, c in zip(tiles_split["row"], tiles_split["col"], strict=True)
    }
    cand = feats[
        feats["stratum"].fillna("").astype(str).ne("")
        & (feats["aoi_coverage"] >= min_aoi_coverage)
        & ~feats["tile_id"].astype(str).isin(set(tiles_split["tile_id"].astype(str)))
    ].copy()
    cand = cand[
        [
            not _is_adjacent((int(r), int(c)), occupied)
            for r, c in zip(cand["row"], cand["col"], strict=True)
        ]
    ]
    cand = cand.sort_values(["stratum", "row", "col"]).reset_index(drop=True)
    counts = {s: int((cand["stratum"] == s).sum()) for s in STRATA if (cand["stratum"] == s).any()}
    alloc = _allocate(counts, n_tiles, min_per_stratum)
    chosen: list[dict[str, Any]] = []
    for s in sorted(counts):
        rows = cand[cand["stratum"] == s].to_dict("records")
        taken = 0
        for i in rng.permutation(len(rows)):
            rc = (int(rows[i]["row"]), int(rows[i]["col"]))
            if taken >= alloc[s] or rc in occupied or _is_adjacent(rc, occupied):
                continue
            chosen.append({**rows[i], "split": "test"})
            occupied.add(rc)
            taken += 1
    if not chosen:
        return pd.DataFrame(columns=TILES_SPLIT_COLUMNS)
    out = pd.DataFrame(chosen)[TILES_SPLIT_COLUMNS].astype({"row": int, "col": int})
    return out.sort_values(["stratum", "row", "col"]).reset_index(drop=True)


def pick_recheck(
    tiles_split: pd.DataFrame, fraction: float, min_tiles: int, seed: int
) -> pd.DataFrame:
    """Deterministic blind re-annotation subset, spread over splits and strata."""
    n = len(tiles_split)
    if n == 0:
        return tiles_split.iloc[0:0][["tile_id", "split", "stratum"]]
    k = min(n, max(min_tiles, math.ceil(n * fraction)))
    rng = np.random.default_rng(seed + 1)
    rows = tiles_split.to_dict("records")
    perm = rng.permutation(n)
    chosen: list[dict[str, Any]] = []
    used: set[tuple[str, str]] = set()
    for i in perm:  # first pass: distinct (split, stratum) combos
        r = rows[i]
        key = (r["split"], r["stratum"])
        if key not in used and len(chosen) < k:
            chosen.append(r)
            used.add(key)
    chosen_ids = {r["tile_id"] for r in chosen}
    for i in perm:  # second pass: fill up
        if len(chosen) >= k:
            break
        r = rows[i]
        if r["tile_id"] not in chosen_ids:
            chosen.append(r)
            chosen_ids.add(r["tile_id"])
    out = pd.DataFrame(chosen)[["tile_id", "split", "stratum"]]
    return out.sort_values(["split", "tile_id"]).reset_index(drop=True)


def select_tiles(
    cfg: dict[str, Any],
    paths: Paths,
    osm_path: Path,
    n_tiles: int | None = None,
    seed: int | None = None,
    test_fraction: float | None = None,
) -> dict[str, Any]:
    """End-to-end: read tiles + OSM, stratify, sample, split, write CSVs and a report."""
    acfg = cfg["annotation"]
    scfg = acfg.get("select_tiles", {})
    strata_cfg = {**DEFAULT_STRATA_CFG, **scfg.get("strata", {})}
    n_tiles = int(n_tiles or acfg.get("n_tiles", 24))
    seed = int(acfg.get("seed", 42) if seed is None else seed)
    test_fraction = float(
        acfg.get("test_fraction", 0.5) if test_fraction is None else test_fraction
    )
    min_buildings = int(scfg.get("min_buildings_per_tile", 3))
    min_cov = float(scfg.get("min_aoi_coverage", 0.9))

    tiles = read_tiles(paths.tiles_index)
    osm = read_vector(osm_path)
    aoi = read_vector(paths.aoi_geojson) if paths.aoi_geojson.exists() else None
    crs_m = metric_crs(cfg, tiles.geometry, paths.manifest)

    feats = tile_features(tiles, osm, crs_m, strata_cfg, aoi)
    feats["stratum"] = [
        classify_tile(r, strata_cfg, min_buildings) for r in feats.to_dict("records")
    ]
    excluded_aoi = int(((feats["aoi_coverage"] < min_cov) & feats["stratum"].notna()).sum())
    feats.loc[feats["aoi_coverage"] < min_cov, "stratum"] = None

    split = sample_tiles(
        feats,
        n_tiles,
        test_fraction,
        seed,
        min_per_stratum=int(scfg.get("min_per_stratum", 3)),
        avoid_adjacent=bool(scfg.get("avoid_adjacent", True)),
    )
    recheck = pick_recheck(
        split, float(acfg.get("recheck_fraction", 0.1)), int(acfg.get("recheck_min_tiles", 2)), seed
    )

    paths.labels_dir.mkdir(parents=True, exist_ok=True)
    split.to_csv(paths.tiles_split_csv, index=False)
    recheck.to_csv(paths.recheck_tiles_csv, index=False)
    feats_out = feats.copy()
    feats_out["stratum"] = feats_out["stratum"].fillna("")
    feats_out.round(4).to_csv(paths.labels_dir / "tiles_features.csv", index=False)

    report = {
        "n_tiles_total": int(len(tiles)),
        "n_osm_buildings": int(len(osm)),
        "metric_crs": crs_m,
        "n_candidates": int(feats["stratum"].notna().sum()),
        "excluded_few_buildings": int((feats["n_buildings"] < min_buildings).sum()),
        "excluded_aoi_coverage": excluded_aoi,
        "candidates_by_stratum": {s: int((feats["stratum"] == s).sum()) for s in STRATA},
        "selected": int(len(split)),
        "selected_by_split": {s: int((split["split"] == s).sum()) for s in ("val", "test")},
        "selected_by_stratum": {s: int((split["stratum"] == s).sum()) for s in STRATA},
        "recheck_tiles": recheck["tile_id"].tolist(),
        "params": {
            "n_tiles": n_tiles,
            "test_fraction": test_fraction,
            "seed": seed,
            "min_buildings_per_tile": min_buildings,
            "min_aoi_coverage": min_cov,
            "aoi_used": aoi is not None,
            "strata": strata_cfg,
        },
        "outputs": {
            "tiles_split": str(paths.tiles_split_csv),
            "recheck_tiles": str(paths.recheck_tiles_csv),
            "tiles_features": str(paths.labels_dir / "tiles_features.csv"),
        },
    }
    (paths.labels_dir / "select_tiles_report.json").write_text(
        stable_json(report), encoding="utf-8"
    )
    return report
