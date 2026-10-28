"""Verification sample of discrepancies and precision metrics from the student's verdicts.

Sampling protocol (``verification-sample``) and formulas (``discrepancies --verification``) are
spelled out in ``src/geoqc/eval/README.md``.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd

from geoqc.eval.common import DISCREPANCY_CLASSES, VERDICTS, EvalInputError

SAMPLE_COLUMNS: tuple[str, ...] = (
    "id",
    "class",
    "confidence",
    "iou",
    "lon",
    "lat",
    "verdict",
    "comment",
)
_BASE_COLUMNS: tuple[str, ...] = ("id", "class", "confidence", "iou", "lon", "lat")


# --------------------------------------------------------------------------- sources
def fetch_discrepancies_db(conn: Any, model_version: str | None = None) -> pd.DataFrame:
    """All rows of ``discrepancies`` with a representative point (lon/lat, EPSG:4326)."""
    sql = (
        "SELECT id, class, confidence, iou, status, model_version, "
        "ST_X(ST_PointOnSurface(geom)) AS lon, ST_Y(ST_PointOnSurface(geom)) AS lat "
        "FROM discrepancies"
    )
    params: tuple[Any, ...] = ()
    if model_version:
        sql += " WHERE model_version = %s"
        params = (model_version,)
    sql += " ORDER BY confidence DESC, id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    df = pd.DataFrame(
        rows, columns=["id", "class", "confidence", "iou", "status", "model_version", "lon", "lat"]
    )
    return _normalise(df)


def fetch_discrepancies_geojson(path: Path) -> pd.DataFrame:
    """Rows from ``reports/discrepancies.geojson`` (CONTRACTS §3.8) as a fallback to the DB."""
    if not Path(path).exists():
        raise EvalInputError(f"discrepancies export not found: {path}")
    gdf = gpd.read_file(path)
    if len(gdf) == 0:
        return pd.DataFrame(columns=list(_BASE_COLUMNS))
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    pts = gdf.geometry.representative_point()
    df = pd.DataFrame(
        {
            "id": gdf["id"] if "id" in gdf.columns else np.arange(1, len(gdf) + 1),
            "class": gdf["class"],
            "confidence": gdf["confidence"],
            "iou": gdf["iou"] if "iou" in gdf.columns else np.nan,
            "status": gdf["status"] if "status" in gdf.columns else "new",
            "model_version": gdf["model_version"] if "model_version" in gdf.columns else None,
            "lon": pts.x.to_numpy(),
            "lat": pts.y.to_numpy(),
        }
    )
    return _normalise(df)


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["id"] = df["id"].astype(int)
    df["class"] = df["class"].astype(str)
    df["confidence"] = df["confidence"].astype(float)
    df["iou"] = pd.to_numeric(df["iou"], errors="coerce")
    unknown = sorted(set(df["class"]) - set(DISCREPANCY_CLASSES))
    if unknown:
        raise EvalInputError(f"unknown discrepancy classes: {unknown}")
    return df.sort_values(["confidence", "id"], ascending=[False, True]).reset_index(drop=True)


def counts_by_class(df: pd.DataFrame) -> dict[str, int]:
    return {cls: int((df["class"] == cls).sum()) if len(df) else 0 for cls in DISCREPANCY_CLASSES}


# --------------------------------------------------------------------------- sampling
def stratified_sample(
    df: pd.DataFrame,
    *,
    sample_size: int = 50,
    per_class_min: int = 15,
    top_k_global: int = 20,
    seed: int = 42,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Select the verification sample; returns (rows, meta).

    1. the ``top_k_global`` most confident discrepancies of the whole queue (Precision@N head);
    2. for each class with fewer than ``per_class_min`` selected: half of the deficit (rounded up)
       from the next most confident items of the class, the rest at random (seeded) from the class;
       a class with fewer items than ``per_class_min`` is taken entirely;
    3. random fill (seeded) from what is left until ``sample_size`` is reached.
    The result therefore has ``max(sample_size, size after step 2)`` rows.
    """
    if df.empty:
        raise EvalInputError("no discrepancies available for sampling")
    df = df.sort_values(["confidence", "id"], ascending=[False, True]).reset_index(drop=True)
    rng = np.random.default_rng(seed)
    reason: dict[int, str] = {}

    for rid in df["id"].head(top_k_global):
        reason[int(rid)] = "top_global"

    for cls in DISCREPANCY_CLASSES:
        sub = df[df["class"] == cls]
        have = int(sub["id"].isin(reason).sum())
        deficit = per_class_min - have
        if deficit <= 0 or sub.empty:
            continue
        rest = sub[~sub["id"].isin(reason)]
        if len(rest) <= deficit:
            for rid in rest["id"]:
                reason[int(rid)] = "class_all"
            continue
        n_top = math.ceil(deficit / 2)
        for rid in rest["id"].head(n_top):
            reason[int(rid)] = "class_top"
        remaining = rest.iloc[n_top:]
        n_rand = deficit - n_top
        if n_rand > 0:
            for rid in remaining.sample(n=n_rand, random_state=rng)["id"]:
                reason[int(rid)] = "class_random"

    if len(reason) < sample_size:
        pool = df[~df["id"].isin(reason)]
        n_fill = min(sample_size - len(reason), len(pool))
        if n_fill > 0:
            for rid in pool.sample(n=n_fill, random_state=rng)["id"]:
                reason[int(rid)] = "fill_random"

    out = df[df["id"].isin(reason)].copy()
    out = out.sort_values(["class", "confidence", "id"], ascending=[True, False, True]).reset_index(
        drop=True
    )
    reasons = pd.Series([reason[int(i)] for i in out["id"]], index=out.index)
    meta = {
        "sample_size_target": sample_size,
        "per_class_min": per_class_min,
        "top_k_global": top_k_global,
        "seed": seed,
        "n_available": int(len(df)),
        "n_selected": int(len(out)),
        "available_by_class": counts_by_class(df),
        "selected_by_class": counts_by_class(out),
        "selected_by_reason": {k: int(v) for k, v in reasons.value_counts().sort_index().items()},
        "short_classes": [
            cls for cls in DISCREPANCY_CLASSES if counts_by_class(out)[cls] < per_class_min
        ],
    }
    return out, meta


def write_sample_csv(df: pd.DataFrame, path: Path) -> None:
    """Write ``verification_sample.csv`` with the fixed columns; verdict/comment left empty."""
    out = pd.DataFrame({c: df[c] for c in _BASE_COLUMNS})
    out["verdict"] = ""
    out["comment"] = ""
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, columns=list(SAMPLE_COLUMNS), float_format="%.6f")


# --------------------------------------------------------------------------- verification
def reanchor_ids(verified: pd.DataFrame, queue: pd.DataFrame, ndigits: int = 6) -> dict[int, int]:
    """Old id -> current id for verified rows missing from a rebuilt queue.

    ``matching run`` recreates ``discrepancies`` with new ids; the same discrepancy keeps its class
    and representative point, so it is found again by (class, lon, lat). Ambiguous keys are skipped.
    """
    if not {"lon", "lat"} <= set(verified.columns) or not {"lon", "lat"} <= set(queue.columns):
        return {}

    def key(df: pd.DataFrame) -> list[tuple[str, float, float]]:
        return list(
            zip(df["class"], df["lon"].round(ndigits), df["lat"].round(ndigits), strict=True)
        )

    q = queue.assign(_k=key(queue))
    q = q[~q["_k"].duplicated(keep=False)].set_index("_k")["id"]
    lost = verified[~verified["id"].isin(queue["id"])]
    found = {
        int(old): int(q[k]) for old, k in zip(lost["id"], key(lost), strict=True) if k in q.index
    }
    # never create a duplicate id in the verification file
    taken = set(verified["id"].astype(int)) - set(found)
    targets = pd.Series(list(found.values()), dtype="int64")
    clash = set(targets[targets.duplicated(keep=False)]) | taken
    return {old: new for old, new in found.items() if new not in clash}


def read_verification_csv(path: Path) -> pd.DataFrame:
    """Read the filled ``verification.csv``; verdicts are normalised (strip/lower), empty -> NaN."""
    if not Path(path).exists():
        raise EvalInputError(f"verification file not found: {path}")
    df = pd.read_csv(path, dtype={"verdict": str, "comment": str, "class": str})
    missing = {"id", "class", "confidence", "verdict"} - set(df.columns)
    if missing:
        raise EvalInputError(
            f"{path} lacks columns {sorted(missing)}; expected {list(SAMPLE_COLUMNS)}"
        )
    df["verdict"] = df["verdict"].fillna("").astype(str).str.strip().str.lower().replace("", np.nan)
    df["confidence"] = df["confidence"].astype(float)
    df["id"] = df["id"].astype(int)
    return df


def _block(sub: pd.DataFrame) -> dict[str, Any]:
    n = int(len(sub))
    counts = {v: int((sub["verdict"] == v).sum()) for v in VERDICTS}
    denominator = n - counts["unsure"]
    return {
        "n": n,
        "unsure": counts["unsure"],
        "true_error": counts["map_error"],
        "model_error": counts["model_error"],
        "image_issue": counts["image_issue"],
        "precision": (counts["map_error"] / denominator) if denominator > 0 else None,
    }


def verification_metrics(
    df: pd.DataFrame, precision_at_n: list[int], verdicts: tuple[str, ...] = VERDICTS
) -> dict[str, Any]:
    """Precision per class / overall and Precision@N from the verified rows.

    precision = map_error / (n - unsure). Precision@N ranks the checked rows by confidence DESC and
    applies the same formula to the top N (``value``); ``value_incl_unsure`` = map_error / N.
    Rows with an empty verdict are not checked and are excluded; unknown verdicts are reported.
    """
    verdict = df["verdict"] if "verdict" in df.columns else pd.Series(np.nan, index=df.index)
    verdict = verdict.fillna("").astype(str).str.strip().str.lower()
    valid_mask = verdict.isin(verdicts)
    invalid = sorted(set(verdict[(verdict != "") & ~valid_mask]))
    checked = df[valid_mask].copy()
    checked["verdict"] = verdict[valid_mask]
    by_class = {cls: _block(checked[checked["class"] == cls]) for cls in DISCREPANCY_CLASSES}
    overall = _block(checked)
    ranked = checked.sort_values(["confidence", "id"], ascending=[False, True])
    at_n: dict[str, Any] = {}
    for n in precision_at_n:
        top = ranked.head(int(n))
        block = _block(top)
        at_n[str(int(n))] = {
            "n": int(n),
            "n_available": int(len(top)),
            "complete": bool(len(top) >= int(n)),
            "true_error": block["true_error"],
            "unsure": block["unsure"],
            "value": block["precision"],
            "value_incl_unsure": (block["true_error"] / len(top)) if len(top) else None,
        }
    return {
        "n_rows": int(len(df)),
        "n_checked": int(len(checked)),
        "n_unchecked": int(len(df) - len(checked)),
        "invalid_verdicts": invalid,
        "verified_by_class": by_class,
        "precision_overall": overall["precision"],
        "overall": overall,
        "precision_at_n": at_n,
    }
