"""Validation of the reference labels and the G2 agreement metric (CONTRACTS §3.5, §4).

``validate_split`` checks ``data/labels/<split>/buildings.geojson`` (geometry validity, property set,
``verified=true`` everywhere, minimal area, duplicates/overlaps, tile membership), computes the
blind re-annotation agreement against ``recheck.geojson`` and writes ``reports/annotation.json``.
Exit code is non-zero when the labels are not accepted.

G2 definitions (see README for the worked example):

* 1:1 pairs between first and re-annotation on the recheck tiles are formed greedily by
  descending IoU over all pairs with IoU > ``pair_min_iou`` (default: any overlap).
* ``agreement_iou`` = mean IoU over those pairs (shifted squares with IoU 1/3 give exactly 1/3).
* ``agreement_f1`` = 2·TP / (2·TP + FP + FN) where TP = pairs with IoU ≥ ``iou_match`` (0.5),
  FP = re-annotation objects without such a pair, FN = first-annotation objects without one.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry.base import BaseGeometry

from geoqc.annotation.common import (
    LABEL_PROPERTIES,
    SOURCES,
    Paths,
    as_bool,
    check_split,
    metric_crs,
    read_tiles,
    read_tiles_split,
    read_vector,
    split_tiles,
    stable_json,
)

MAX_LISTED_ISSUES = 300


# --------------------------------------------------------------------------- geometry metrics


def iou(a: BaseGeometry, b: BaseGeometry) -> float:
    """Intersection over union of two (metric) geometries; 0 for empty union."""
    inter = a.intersection(b).area
    if inter <= 0:
        return 0.0
    union = a.union(b).area
    return float(inter / union) if union > 0 else 0.0


def match_pairs(
    first: list[BaseGeometry], second: list[BaseGeometry], min_iou: float = 0.0
) -> list[tuple[int, int, float]]:
    """Greedy 1:1 matching by descending IoU; only pairs with IoU > ``min_iou`` are candidates."""
    if not first or not second:
        return []
    tree = shapely.STRtree(second)
    f_idx, s_idx = tree.query(first, predicate="intersects")
    cands = []
    for i, j in zip(f_idx, s_idx, strict=True):
        v = iou(first[i], second[j])
        if v > min_iou:
            cands.append((v, int(i), int(j)))
    cands.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_f: set[int] = set()
    used_s: set[int] = set()
    pairs = []
    for v, i, j in cands:
        if i in used_f or j in used_s:
            continue
        used_f.add(i)
        used_s.add(j)
        pairs.append((i, j, v))
    return pairs


def agreement_metrics(
    first: list[BaseGeometry],
    second: list[BaseGeometry],
    iou_match: float = 0.5,
    pair_min_iou: float = 0.0,
) -> dict[str, Any]:
    """Mean IoU of 1:1 pairs and F1 at ``iou_match`` between two annotations of the same area."""
    pairs = match_pairs(first, second, pair_min_iou)
    ious = [p[2] for p in pairs]
    tp = sum(1 for v in ious if v >= iou_match)
    fp = len(second) - tp
    fn = len(first) - tp
    f1 = (
        2 * tp / (2 * tp + fp + fn)
        if (2 * tp + fp + fn) > 0
        else (1.0 if not first and not second else 0.0)
    )
    return {
        "n_first": len(first),
        "n_recheck": len(second),
        "n_pairs": len(pairs),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "mean_iou_pairs": float(np.mean(ious)) if ious else None,
        "mean_iou_matched": float(np.mean([v for v in ious if v >= iou_match])) if tp else None,
        "f1": float(f1),
    }


# --------------------------------------------------------------------------- label checks


def _issue(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def check_labels(
    labels: gpd.GeoDataFrame,
    tiles: gpd.GeoDataFrame,
    split: str,
    crs_metric: str,
    min_area_m2: float,
    vcfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return (errors, warnings, stats) for one labels frame against the tiles of its split."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    tile_ids = set(tiles["tile_id"].astype(str))
    tiles_m = tiles.to_crs(crs_metric).set_index("tile_id")
    tol = float(vcfg.get("tile_tolerance_m", 0.5))
    overlap_ratio = float(vcfg.get("overlap_error_ratio", 0.5))
    overlap_warn = float(vcfg.get("overlap_warn_m2", 1.0))

    for prop in LABEL_PROPERTIES:
        if prop not in labels.columns:
            errors.append(
                _issue("missing_property", f"property {prop!r} is missing in all features")
            )
    if errors:
        return errors, warnings, {"n_features": int(len(labels))}

    lab_m = labels.to_crs(crs_metric)
    geoms = lab_m.geometry.values
    n = len(lab_m)
    areas = np.zeros(n)
    verified_flags = []
    for i in range(n):
        row = lab_m.iloc[i]
        fid = i
        tile_id = str(row["tile_id"]) if pd.notna(row["tile_id"]) else ""
        g = geoms[i]
        v = as_bool(row["verified"])
        verified_flags.append(v is True)
        if v is not True:
            errors.append(
                _issue("not_verified", "verified is not true", feature=fid, tile_id=tile_id)
            )
        if row["source"] not in SOURCES:
            errors.append(
                _issue(
                    "bad_source",
                    f"source={row['source']!r} not in {SOURCES}",
                    feature=fid,
                    tile_id=tile_id,
                )
            )
        if str(row["split"]) != split:
            errors.append(
                _issue(
                    "bad_split",
                    f"split={row['split']!r} != {split!r}",
                    feature=fid,
                    tile_id=tile_id,
                )
            )
        if tile_id not in tile_ids:
            errors.append(
                _issue(
                    "unknown_tile",
                    f"tile_id={tile_id!r} is not a {split} tile",
                    feature=fid,
                    tile_id=tile_id,
                )
            )
        if g is None or g.is_empty:
            errors.append(_issue("empty_geometry", "empty geometry", feature=fid, tile_id=tile_id))
            continue
        if g.geom_type not in ("Polygon", "MultiPolygon"):
            errors.append(
                _issue(
                    "geometry_type", f"geometry type {g.geom_type}", feature=fid, tile_id=tile_id
                )
            )
            continue
        if not g.is_valid:
            errors.append(
                _issue("invalid_geometry", shapely.is_valid_reason(g), feature=fid, tile_id=tile_id)
            )
            continue
        areas[i] = g.area
        if g.area < min_area_m2:
            errors.append(
                _issue(
                    "min_area",
                    f"area {g.area:.1f} m² < {min_area_m2} m²",
                    feature=fid,
                    tile_id=tile_id,
                )
            )
        if tile_id in tile_ids:
            outside = g.difference(tiles_m.loc[tile_id, "geometry"].buffer(tol)).area
            if outside > 0.01:
                errors.append(
                    _issue(
                        "outside_tile",
                        f"{outside:.1f} m² outside tile {tile_id} (run `geoqc annotation finalize`)",
                        feature=fid,
                        tile_id=tile_id,
                    )
                )

    # overlaps / duplicates (pairs i<j with a real intersection)
    valid_mask = np.array([g is not None and not g.is_empty and g.is_valid for g in geoms])
    if valid_mask.sum() > 1:
        idx = np.flatnonzero(valid_mask)
        sub = [geoms[k] for k in idx]
        a_idx, b_idx = shapely.STRtree(sub).query(sub, predicate="intersects")
        for a, b in zip(a_idx, b_idx, strict=True):
            if a >= b:
                continue
            inter = sub[a].intersection(sub[b]).area
            if inter <= 0:
                continue
            i, j = int(idx[a]), int(idx[b])
            ratio = inter / max(min(areas[i], areas[j]), 1e-9)
            if ratio > overlap_ratio:
                errors.append(
                    _issue(
                        "duplicate_overlap",
                        f"features {i} and {j} overlap by {ratio:.0%} of the smaller one",
                        feature=i,
                        other=j,
                        tile_id=str(lab_m.iloc[i]["tile_id"]),
                    )
                )
            elif inter > overlap_warn:
                warnings.append(
                    _issue(
                        "small_overlap",
                        f"features {i} and {j} overlap by {inter:.1f} m²",
                        feature=i,
                        other=j,
                        tile_id=str(lab_m.iloc[i]["tile_id"]),
                    )
                )

    per_tile = labels.groupby(labels["tile_id"].astype(str)).size().to_dict() if n else {}
    empty_tiles = sorted(tile_ids - set(per_tile))
    for t in empty_tiles:
        warnings.append(
            _issue("tile_without_labels", f"tile {t} has no buildings labelled", tile_id=t)
        )
    stats = {
        "n_features": int(n),
        "n_verified": int(sum(verified_flags)),
        "n_tiles": int(len(tile_ids)),
        "n_tiles_with_labels": int(len(per_tile)),
        "tiles_without_labels": empty_tiles,
        "per_tile": {k: int(v) for k, v in sorted(per_tile.items())},
        "area_m2": {
            "min": float(areas[areas > 0].min()) if (areas > 0).any() else None,
            "median": float(np.median(areas[areas > 0])) if (areas > 0).any() else None,
            "max": float(areas.max()) if n else None,
        },
        "sources": {k: int(v) for k, v in labels["source"].value_counts().items()},
    }
    return errors, warnings, stats


def assign_tile_ids(
    gdf: gpd.GeoDataFrame, tiles: gpd.GeoDataFrame, crs_metric: str
) -> gpd.GeoDataFrame:
    """Fill missing/foreign ``tile_id`` by the tile with the largest intersection area."""
    out = gdf.copy()
    if "tile_id" not in out.columns:
        out["tile_id"] = ""
    out["tile_id"] = out["tile_id"].fillna("").astype(str)
    tile_ids = set(tiles["tile_id"].astype(str))
    need = out.index[~out["tile_id"].isin(tile_ids)].tolist()
    if not need:
        return out
    tiles_m = tiles.to_crs(crs_metric).reset_index(drop=True)
    g_m = out.loc[need].to_crs(crs_metric).geometry.values
    tree = shapely.STRtree(tiles_m.geometry.values)
    g_idx, t_idx = tree.query(g_m, predicate="intersects")
    best: dict[int, tuple[float, str]] = {}
    for gi, ti in zip(g_idx, t_idx, strict=True):
        a = shapely.intersection(g_m[gi], tiles_m.geometry.values[ti]).area
        if a > best.get(gi, (-1.0, ""))[0]:
            best[gi] = (a, str(tiles_m.loc[ti, "tile_id"]))
    for k, fid in enumerate(need):
        if k in best:
            out.loc[fid, "tile_id"] = best[k][1]
    return out


def compute_agreement(
    labels: gpd.GeoDataFrame,
    recheck: gpd.GeoDataFrame,
    tiles: gpd.GeoDataFrame,
    recheck_tile_ids: list[str],
    crs_metric: str,
    iou_match: float,
    pair_min_iou: float,
) -> dict[str, Any]:
    """G2 on the recheck tiles: overall (pooled objects) and per tile."""
    recheck = assign_tile_ids(recheck, tiles, crs_metric)
    lab_m = labels.to_crs(crs_metric)
    rec_m = recheck.to_crs(crs_metric)
    lab_m = lab_m[lab_m.geometry.notna() & lab_m.geometry.is_valid]
    rec_m = rec_m[rec_m.geometry.notna() & rec_m.geometry.is_valid]
    per_tile = {}
    first_all: list[BaseGeometry] = []
    second_all: list[BaseGeometry] = []
    for t in recheck_tile_ids:
        f = list(lab_m[lab_m["tile_id"].astype(str) == t].geometry.values)
        s = list(rec_m[rec_m["tile_id"].astype(str) == t].geometry.values)
        per_tile[t] = agreement_metrics(f, s, iou_match, pair_min_iou)
        first_all.extend(f)
        second_all.extend(s)
    overall = agreement_metrics(first_all, second_all, iou_match, pair_min_iou)
    return {
        "recheck_tiles": list(recheck_tile_ids),
        "n_checked": len(recheck_tile_ids),
        "agreement_iou": overall["mean_iou_pairs"],
        "agreement_f1": overall["f1"],
        "overall": overall,
        "per_tile": per_tile,
        "iou_match": iou_match,
        "pair_min_iou": pair_min_iou,
    }


# --------------------------------------------------------------------------- report assembly


def _load_report(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {"schema_version": "1.0", "splits": {}}


def _aggregate(report: dict[str, Any], gate_cfg: dict[str, Any] | None) -> None:
    """Pool G2 over splits with agreement and fill ``annotation`` + ``gates.G2``."""
    ious: list[float] = []
    weights: list[int] = []
    tp = fp = fn = 0
    n_checked = 0
    for s in report.get("splits", {}).values():
        agr = s.get("agreement")
        if not agr:
            continue
        ov = agr["overall"]
        n_checked += agr["n_checked"]
        tp, fp, fn = tp + ov["tp"], fp + ov["fp"], fn + ov["fn"]
        if ov["mean_iou_pairs"] is not None and ov["n_pairs"] > 0:
            ious.append(ov["mean_iou_pairs"])
            weights.append(ov["n_pairs"])
    agreement_iou = float(np.average(ious, weights=weights)) if ious else None
    denom = 2 * tp + fp + fn
    agreement_f1 = (2 * tp / denom) if denom > 0 else None
    report["annotation"] = {
        "agreement_iou": agreement_iou,
        "agreement_f1": agreement_f1,
        "n_checked": n_checked,
    }
    target = float((gate_cfg or {}).get("target", 0.8))
    op = str((gate_cfg or {}).get("op", ">="))
    passed = agreement_iou is not None and _compare(agreement_iou, op, target)
    note = "" if agreement_iou is not None else "recheck.geojson missing: G2 not computed"
    report["gates"] = {
        "G2": {
            "target": target,
            "op": op,
            "actual": agreement_iou,
            "passed": bool(passed),
            "note": note,
        }
    }


def _compare(actual: float, op: str, target: float) -> bool:
    return {
        ">=": actual >= target,
        ">": actual > target,
        "<=": actual <= target,
        "<": actual < target,
        "==": actual == target,
    }.get(op, False)


def validate_split(
    cfg: dict[str, Any],
    paths: Paths,
    split: str,
    labels_path: Path | None = None,
    recheck_path: Path | None = None,
    report_path: Path | None = None,
    strict: bool = False,
) -> tuple[dict[str, Any], int]:
    """Validate one split, update ``reports/annotation.json``; return (report, exit_code)."""
    check_split(split)
    acfg = cfg["annotation"]
    vcfg = acfg.get("validate", {})
    min_area = float(acfg.get("min_area_m2", 12.0))
    labels_path = labels_path or paths.split_dir(split) / "buildings.geojson"
    recheck_path = recheck_path or paths.split_dir(split) / "recheck.geojson"
    report_path = report_path or paths.reports_dir / "annotation.json"

    report = _load_report(report_path)
    report["schema_version"] = "1.0"
    report["generated_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    report["config"] = {
        "min_area_m2": min_area,
        "iou_match": float(vcfg.get("iou_match", 0.5)),
        "pair_min_iou": float(vcfg.get("pair_min_iou", 0.0)),
        "overlap_error_ratio": float(vcfg.get("overlap_error_ratio", 0.5)),
        "tile_tolerance_m": float(vcfg.get("tile_tolerance_m", 0.5)),
        "recheck_fraction": float(acfg.get("recheck_fraction", 0.1)),
    }
    entry: dict[str, Any] = {"labels": str(labels_path), "recheck": str(recheck_path)}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    tiles_all = read_tiles(paths.tiles_index)
    tiles_s = split_tiles(tiles_all, read_tiles_split(paths.tiles_split_csv), split)
    crs_m = metric_crs(cfg, tiles_all.geometry, paths.manifest)
    entry["metric_crs"] = crs_m

    if not labels_path.exists():
        errors.append(_issue("file_missing", f"{labels_path} not found"))
        entry.update(status="missing", errors=errors, warnings=warnings, stats={"n_features": 0})
    else:
        labels = read_vector(labels_path)
        errors, warnings, stats = check_labels(labels, tiles_s, split, crs_m, min_area, vcfg)
        entry["stats"] = stats

        # ---- G2
        expected_recheck: list[str] = []
        if paths.recheck_tiles_csv.exists():
            rc = pd.read_csv(paths.recheck_tiles_csv, dtype=str)
            expected_recheck = sorted(rc.loc[rc["split"] == split, "tile_id"].tolist())
        if recheck_path.exists():
            recheck = read_vector(recheck_path)
            recheck = assign_tile_ids(recheck, tiles_s, crs_m)
            present = sorted(set(recheck["tile_id"].astype(str)) & set(tiles_s["tile_id"]))
            recheck_tiles = sorted(set(expected_recheck) | set(present))
            if expected_recheck and set(present) - set(expected_recheck):
                warnings.append(
                    _issue(
                        "recheck_tiles_mismatch",
                        f"recheck has tiles not in recheck_tiles.csv: {sorted(set(present) - set(expected_recheck))}",
                    )
                )
            if expected_recheck and set(expected_recheck) - set(present):
                warnings.append(
                    _issue(
                        "recheck_tiles_missing",
                        f"recheck tiles without re-annotated objects: {sorted(set(expected_recheck) - set(present))}",
                    )
                )
            if recheck_tiles:
                entry["agreement"] = compute_agreement(
                    labels,
                    recheck,
                    tiles_s,
                    recheck_tiles,
                    crs_m,
                    float(vcfg.get("iou_match", 0.5)),
                    float(vcfg.get("pair_min_iou", 0.0)),
                )
            else:
                warnings.append(
                    _issue("recheck_empty", "recheck.geojson has no objects on this split's tiles")
                )
        else:
            warnings.append(
                _issue(
                    "recheck_missing", f"{recheck_path.name} not found: G2 not computed for {split}"
                )
            )
            entry["agreement"] = None
        entry["status"] = "accepted" if not errors else "rejected"

    report.setdefault("splits", {})[split] = entry
    _aggregate(report, (cfg.get("gates") or {}).get("G2"))
    g2_failed = bool(entry.get("agreement")) and not report["gates"]["G2"]["passed"]
    if g2_failed:
        warnings.append(
            _issue(
                "g2_below_target",
                f"G2 agreement_iou={report['annotation']['agreement_iou']} below target",
            )
        )
    entry["errors_by_code"] = _count_by_code(errors)
    entry["warnings_by_code"] = _count_by_code(warnings)
    entry["errors"] = errors[:MAX_LISTED_ISSUES]
    entry["warnings"] = warnings[:MAX_LISTED_ISSUES]
    entry["n_errors"] = len(errors)
    entry["n_warnings"] = len(warnings)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(stable_json(report), encoding="utf-8")
    exit_code = 1 if errors else 0
    if strict and (warnings or g2_failed):
        exit_code = max(exit_code, 2)
    return report, exit_code


def _count_by_code(issues: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in issues:
        out[it["code"]] = out.get(it["code"], 0) + 1
    return dict(sorted(out.items()))


def format_summary(report: dict[str, Any], split: str) -> str:
    """Human-readable summary of one split for the terminal."""
    e = report["splits"][split]
    lines = [f"[{split}] status: {e.get('status')}"]
    st = e.get("stats") or {}
    if st:
        lines.append(
            f"  features: {st.get('n_features', 0)}  verified: {st.get('n_verified', 0)}  "
            f"tiles with labels: {st.get('n_tiles_with_labels', 0)}/{st.get('n_tiles', 0)}"
        )
    if e.get("errors_by_code"):
        lines.append("  errors: " + ", ".join(f"{k}={v}" for k, v in e["errors_by_code"].items()))
        for it in e["errors"][:10]:
            loc = (
                f" (feature {it['feature']}, tile {it.get('tile_id', '')})"
                if "feature" in it
                else ""
            )
            lines.append(f"    - {it['code']}: {it['message']}{loc}")
        if e["n_errors"] > 10:
            lines.append(f"    ... {e['n_errors'] - 10} more, see reports/annotation.json")
    if e.get("warnings_by_code"):
        lines.append(
            "  warnings: " + ", ".join(f"{k}={v}" for k, v in e["warnings_by_code"].items())
        )
    agr = e.get("agreement")
    if agr:
        lines.append(
            f"  G2 on {agr['n_checked']} tile(s): agreement_iou={agr['agreement_iou']}, "
            f"agreement_f1={agr['agreement_f1']:.3f} (tp={agr['overall']['tp']}, fp={agr['overall']['fp']}, fn={agr['overall']['fn']})"
        )
    g2 = report.get("gates", {}).get("G2", {})
    lines.append(
        f"  G2 overall: actual={g2.get('actual')} target={g2.get('op')}{g2.get('target')} passed={g2.get('passed')}"
        + (f" ({g2['note']})" if g2.get("note") else "")
    )
    return "\n".join(lines)
