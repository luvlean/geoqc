"""Error analysis of a split: FP/FN by tile and stratum, area and IoU distributions, raster-based
proxies (shadow, vegetation), tile-border and roof-complexity statistics, figures and markdown.

Every number in ``reports/error_analysis.md`` is computed here and also stored in
``reports/error_analysis.json``; sections without data say so instead of guessing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from geoqc.eval.common import EvalPaths, now_iso, read_git_commit, rel, write_json
from geoqc.eval.detection_metrics import SplitEvaluation, precision_recall_f1

SECTION_TITLES: tuple[str, ...] = (
    "Тени",
    "Сложные крыши",
    "Плотная застройка",
    "Растительность",
    "Граница тайла",
)


# --------------------------------------------------------------------------- small helpers
def histogram(values: list[float] | np.ndarray, edges: list[float]) -> dict[str, Any]:
    arr = np.asarray(values, dtype=float)
    counts, _ = (
        np.histogram(arr, bins=np.asarray(edges, dtype=float))
        if arr.size
        else (np.zeros(len(edges) - 1, int), None)
    )
    return {
        "edges": [float(e) for e in edges],
        "counts": [int(c) for c in counts],
        "n": int(arr.size),
    }


def quantiles(values: list[float] | np.ndarray) -> dict[str, float | int | None]:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return {
            "n": 0,
            "min": None,
            "p25": None,
            "median": None,
            "p75": None,
            "max": None,
            "mean": None,
        }
    return {
        "n": int(arr.size),
        "min": float(arr.min()),
        "p25": float(np.percentile(arr, 25)),
        "median": float(np.median(arr)),
        "p75": float(np.percentile(arr, 75)),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
    }


def convexity(geom: Any) -> float:
    """area / area(convex hull); 1.0 for convex footprints, lower for L/U-shaped ones."""
    hull_area = geom.convex_hull.area
    return float(geom.area / hull_area) if hull_area > 0 else 1.0


def _share(num: int, den: int) -> float | None:
    return float(num / den) if den > 0 else None


# --------------------------------------------------------------------------- statistics blocks
def worst_tiles(per_tile: pd.DataFrame, k: int) -> list[dict[str, Any]]:
    if per_tile.empty:
        return []
    ranked = per_tile.sort_values(
        ["f1", "fp", "fn", "tile_id"], ascending=[True, False, False, True]
    )
    return ranked.head(k).to_dict(orient="records")


def complex_roof_stats(ev: SplitEvaluation, threshold: float) -> dict[str, Any]:
    """FN rate and matched IoU for non-convex ("complex") vs convex GT footprints."""
    if len(ev.gt) == 0:
        return {"threshold": threshold, "n_gt": 0, "note": "no GT objects"}
    conv = np.array([convexity(g) for g in ev.gt.geometry])
    status = ev.gt_status().to_numpy()
    pair_iou = {g: v for g, _, v in ev.match.pairs}
    is_complex = conv < threshold
    out: dict[str, Any] = {"threshold": threshold, "n_gt": int(len(ev.gt))}
    for name, mask in (("complex", is_complex), ("simple", ~is_complex)):
        idx = np.nonzero(mask)[0]
        n = int(mask.sum())
        fn = int((status[mask] == "fn").sum())
        ious = [pair_iou[i] for i in idx if i in pair_iou]
        out[name] = {
            "n": n,
            "fn": fn,
            "fn_rate": _share(fn, n),
            "mean_iou_matched": float(np.mean(ious)) if ious else None,
        }
    out["convexity_median_all"] = float(np.median(conv))
    return out


def tile_border_stats(ev: SplitEvaluation, buffer_m: float) -> dict[str, Any] | None:
    """How many FN/FP (and TP for reference) lie within ``buffer_m`` of their tile edge."""
    if ev.tiles is None or len(ev.tiles) == 0:
        return None
    boundaries = {row.tile_id: row.geometry.boundary for row in ev.tiles.itertuples()}

    def near_border(gdf, statuses) -> dict[str, dict[str, Any]]:
        res: dict[str, dict[str, Any]] = {}
        for st in sorted(set(statuses)):
            mask = statuses == st
            sub = gdf[mask]
            n_border = 0
            n_with_tile = 0
            for tid, geom in zip(sub["tile_id"], sub.geometry, strict=True):
                boundary = boundaries.get(str(tid)) if tid is not None else None
                if boundary is None:
                    continue
                n_with_tile += 1
                if geom.distance(boundary) <= buffer_m:
                    n_border += 1
            res[st] = {
                "n": int(mask.sum()),
                "n_with_tile": n_with_tile,
                "at_border": n_border,
                "share": _share(n_border, n_with_tile),
            }
        return res

    gt_res = near_border(ev.gt, ev.gt_status().to_numpy()) if len(ev.gt) else {}
    det_res = near_border(ev.det, ev.det_status().to_numpy()) if len(ev.det) else {}
    return {
        "buffer_m": buffer_m,
        "fn": gt_res.get("fn", {"n": 0, "n_with_tile": 0, "at_border": 0, "share": None}),
        "tp_gt": gt_res.get("tp", {"n": 0, "n_with_tile": 0, "at_border": 0, "share": None}),
        "fp": det_res.get("fp", {"n": 0, "n_with_tile": 0, "at_border": 0, "share": None}),
        "tp_det": det_res.get("tp", {"n": 0, "n_with_tile": 0, "at_border": 0, "share": None}),
    }


def density_stats(ev: SplitEvaluation, per_tile: pd.DataFrame) -> dict[str, Any]:
    """Detection quality on the densest quartile of tiles vs the rest, plus the dense stratum."""
    out: dict[str, Any] = {"dense_stratum": ev.per_stratum().get("dense_multistorey")}
    if per_tile.empty:
        out["note"] = "no tiles"
        return out
    table = per_tile.copy()
    if ev.tiles is not None and len(ev.tiles):
        areas_ha = dict(zip(ev.tiles["tile_id"], ev.tiles.geometry.area / 10_000.0, strict=True))
        table["density_per_ha"] = [
            (row.n_gt / areas_ha[row.tile_id]) if areas_ha.get(row.tile_id) else np.nan
            for row in table.itertuples()
        ]
        metric = "density_per_ha"
    else:
        table["density_per_ha"] = np.nan
        metric = "n_gt"
    values = table[metric].astype(float)
    if values.notna().sum() < 2:
        out["note"] = "fewer than 2 tiles with density"
        return out
    q75 = float(np.nanpercentile(values, 75))
    dense_mask = values >= q75

    def agg(sub: pd.DataFrame) -> dict[str, Any]:
        tp, fp, fn = int(sub["tp"].sum()), int(sub["fp"].sum()), int(sub["fn"].sum())
        p, r, f1 = precision_recall_f1(tp, fp, fn)
        return {
            "n_tiles": int(len(sub)),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": p,
            "recall": r,
            "f1": f1,
        }

    out.update(
        {
            "density_metric": metric,
            "q75_threshold": q75,
            "densest_quartile": agg(table[dense_mask]),
            "rest": agg(table[~dense_mask]),
            "densest_tiles": table.loc[dense_mask, "tile_id"].tolist(),
        }
    )
    return out


def raster_proxies(
    ev: SplitEvaluation, tiles_dir: Path, shadow_q: float, exg_thr: float
) -> dict[str, Any] | None:
    """Mean luminance / ExG inside every object from the tile rasters; None when no rasters exist."""
    try:
        import rasterio
        import rasterio.mask
    except ImportError:  # pragma: no cover
        return None
    import geopandas as gpd

    gt_status = ev.gt_status().to_numpy() if len(ev.gt) else np.array([])
    det_status = ev.det_status().to_numpy() if len(ev.det) else np.array([])
    lum: dict[str, list[float]] = {"tp_gt": [], "fn": [], "tp_det": [], "fp": []}
    exg: dict[str, list[float]] = {"tp_gt": [], "fn": [], "tp_det": [], "fp": []}
    n_rasters = 0

    def stats(src, geoms):
        vals = []
        for geom in geoms:
            try:
                data, _ = rasterio.mask.mask(src, [geom.__geo_interface__], crop=True, filled=False)
            except (ValueError, IndexError):
                vals.append((np.nan, np.nan))
                continue
            rgb = data[:3].astype(float)
            valid = ~np.ma.getmaskarray(rgb).any(axis=0) & (rgb.filled(0).sum(axis=0) > 0)
            if valid.sum() == 0:
                vals.append((np.nan, np.nan))
                continue
            r, g, b = (rgb[i].filled(0)[valid] for i in range(3))
            vals.append(
                (float(np.mean(0.299 * r + 0.587 * g + 0.114 * b)), float(np.mean(2 * g - r - b)))
            )
        return vals

    for tid in ev.tile_ids:
        tif = Path(tiles_dir) / f"{tid}.tif"
        if not tif.exists():
            continue
        n_rasters += 1
        with rasterio.open(tif) as src:
            for gdf, statuses, keys in (
                (ev.gt, gt_status, ("tp_gt", "fn")),
                (ev.det, det_status, ("tp_det", "fp")),
            ):
                if len(gdf) == 0:
                    continue
                mask = gdf["tile_id"].astype(str).to_numpy() == tid
                if not mask.any():
                    continue
                geoms = gpd.GeoSeries(gdf.geometry[mask].to_numpy(), crs=ev.metric_crs).to_crs(
                    src.crs
                )
                for (l_val, e_val), st in zip(stats(src, geoms), statuses[mask], strict=True):
                    key = keys[0] if st == "tp" else keys[1]
                    if not np.isnan(l_val):
                        lum[key].append(l_val)
                        exg[key].append(e_val)
    if n_rasters == 0:
        return None
    tp_lum = np.asarray(lum["tp_gt"])
    fn_lum = np.asarray(lum["fn"])
    shadow_thr = float(np.quantile(tp_lum, shadow_q)) if tp_lum.size else None
    fp_exg = np.asarray(exg["fp"])
    tp_exg = np.asarray(exg["tp_det"])
    return {
        "n_rasters": n_rasters,
        "shadow": {
            "luminance_quantile": shadow_q,
            "threshold_luminance": shadow_thr,
            "median_luminance_tp": float(np.median(tp_lum)) if tp_lum.size else None,
            "median_luminance_fn": float(np.median(fn_lum)) if fn_lum.size else None,
            "n_fn": int(fn_lum.size),
            "n_fn_dark": int((fn_lum < shadow_thr).sum())
            if shadow_thr is not None and fn_lum.size
            else 0,
            "share_fn_dark": _share(int((fn_lum < shadow_thr).sum()), int(fn_lum.size))
            if shadow_thr is not None
            else None,
        },
        "vegetation": {
            "exg_threshold": exg_thr,
            "median_exg_tp": float(np.median(tp_exg)) if tp_exg.size else None,
            "median_exg_fp": float(np.median(fp_exg)) if fp_exg.size else None,
            "n_fp": int(fp_exg.size),
            "n_fp_green": int((fp_exg > exg_thr).sum()),
            "share_fp_green": _share(int((fp_exg > exg_thr).sum()), int(fp_exg.size)),
            "share_tp_green": _share(int((tp_exg > exg_thr).sum()), int(tp_exg.size)),
        },
    }


# --------------------------------------------------------------------------- figures
def render_tile_figure(
    ev: SplitEvaluation, tile_id: str, tiles_dir: Path, out_path: Path
) -> str | None:
    """PNG: tile raster (if present) + GT (TP green / FN orange) + detections (TP blue / FP red)."""
    import matplotlib

    matplotlib.use("Agg")
    import geopandas as gpd
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    fig, ax = plt.subplots(figsize=(7, 7))
    target_crs = ev.metric_crs
    tif = Path(tiles_dir) / f"{tile_id}.tif"
    if tif.exists():
        import rasterio

        with rasterio.open(tif) as src:
            img = np.transpose(src.read(list(range(1, min(3, src.count) + 1))), (1, 2, 0))
            if img.shape[2] == 1:
                img = np.repeat(img, 3, axis=2)
            b = src.bounds
            ax.imshow(img, extent=(b.left, b.right, b.bottom, b.top))
            target_crs = src.crs
    elif ev.tiles is not None and tile_id in set(ev.tiles["tile_id"]):
        gpd.GeoSeries(
            ev.tiles.loc[ev.tiles["tile_id"] == tile_id, "geometry"].to_numpy(), crs=ev.metric_crs
        ).boundary.plot(ax=ax, color="black", linewidth=0.8)
    gt_mask = ev.gt["tile_id"].astype(str) == tile_id if len(ev.gt) else pd.Series(dtype=bool)
    det_mask = ev.det["tile_id"].astype(str) == tile_id if len(ev.det) else pd.Series(dtype=bool)
    gt_status = ev.gt_status()
    det_status = ev.det_status()
    layers = (
        (ev.gt[gt_mask & (gt_status == "tp")], "#2ca02c", "GT TP"),
        (ev.gt[gt_mask & (gt_status == "fn")], "#ff7f0e", "GT FN"),
        (ev.det[det_mask & (det_status == "tp")], "#1f77b4", "det TP"),
        (ev.det[det_mask & (det_status == "fp")], "#d62728", "det FP"),
    )
    for gdf, color, _ in layers:
        if len(gdf):
            gpd.GeoSeries(gdf.geometry.to_numpy(), crs=ev.metric_crs).to_crs(
                target_crs
            ).boundary.plot(ax=ax, color=color, linewidth=1.2)
    ax.legend(
        handles=[Line2D([0], [0], color=c, lw=2, label=lab) for _, c, lab in layers],
        loc="lower right",
        fontsize=8,
    )
    ax.set_title(f"{ev.split} tile {tile_id}")
    ax.set_axis_off()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return str(out_path)


def render_histograms(analysis: dict[str, Any], figures_dir: Path, split: str) -> dict[str, str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figures_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    iou_h = analysis["iou_histogram"]["matched"]
    fig, ax = plt.subplots(figsize=(6, 3.5))
    ax.bar(
        iou_h["edges"][:-1],
        iou_h["counts"],
        width=np.diff(iou_h["edges"]),
        align="edge",
        color="#1f77b4",
        edgecolor="white",
    )
    ax.set_xlabel("IoU of matched pairs")
    ax.set_ylabel("count")
    ax.set_title(f"IoU histogram ({split})")
    path = figures_dir / f"iou_hist_{split}.png"
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    out["iou_hist"] = str(path)

    fig, ax = plt.subplots(figsize=(6, 3.5))
    labels = [
        f"{int(a)}–{int(b)}" if b < 1e5 else f"≥{int(a)}"
        for a, b in zip(analysis["area_bins_m2"][:-1], analysis["area_bins_m2"][1:], strict=True)
    ]
    x = np.arange(len(labels))
    ax.bar(
        x - 0.2, analysis["area_histogram"]["fp"]["counts"], width=0.4, label="FP", color="#d62728"
    )
    ax.bar(
        x + 0.2, analysis["area_histogram"]["fn"]["counts"], width=0.4, label="FN", color="#ff7f0e"
    )
    ax.set_xticks(x, labels, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel("area, m²")
    ax.set_ylabel("count")
    ax.legend()
    ax.set_title(f"Area of errors ({split})")
    path = figures_dir / f"area_hist_{split}.png"
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    out["area_hist"] = str(path)
    return out


# --------------------------------------------------------------------------- orchestration
def analyse(ev: SplitEvaluation, ecfg: dict[str, Any], tiles_dir: Path) -> dict[str, Any]:
    """All numbers of the report as one JSON-able dict (no figures yet)."""
    ea = ecfg["error_analysis"]
    per_tile = ev.per_tile()
    gt_status = ev.gt_status() if len(ev.gt) else pd.Series(dtype=object)
    det_status = ev.det_status() if len(ev.det) else pd.Series(dtype=object)
    fn_areas = ev.gt.geometry[gt_status == "fn"].area.to_numpy() if len(ev.gt) else np.array([])
    fp_areas = ev.det.geometry[det_status == "fp"].area.to_numpy() if len(ev.det) else np.array([])
    tp_gt_areas = ev.gt.geometry[gt_status == "tp"].area.to_numpy() if len(ev.gt) else np.array([])
    matched_iou = np.asarray(ev.match.ious(), dtype=float)
    # Best IoU of unmatched GT with any detection (0 when nothing overlaps): shows near misses.
    best_unmatched = (
        ev.iou_matrix[ev.match.unmatched_gt].max(axis=1)
        if ev.match.unmatched_gt and ev.iou_matrix.shape[1] > 0
        else np.zeros(len(ev.match.unmatched_gt))
    )
    n_bins = int(ea["iou_bins"])
    iou_edges = list(np.linspace(0.0, 1.0, n_bins + 1))
    area_bins = [float(b) for b in ea["area_bins_m2"]]
    return {
        "split": ev.split,
        "iou_threshold": ev.match.iou_threshold,
        "match_method": ev.match.method,
        "summary": ev.summary(),
        "per_stratum": ev.per_stratum(),
        "per_tile": per_tile.to_dict(orient="records"),
        "worst_tiles": worst_tiles(per_tile, int(ea["top_k_worst_tiles"])),
        "area_bins_m2": area_bins,
        "area_histogram": {
            "fp": histogram(fp_areas, area_bins),
            "fn": histogram(fn_areas, area_bins),
            "tp_gt": histogram(tp_gt_areas, area_bins),
        },
        "area_quantiles": {
            "fp": quantiles(fp_areas),
            "fn": quantiles(fn_areas),
            "tp_gt": quantiles(tp_gt_areas),
        },
        "iou_histogram": {
            "matched": histogram(matched_iou, iou_edges),
            "best_iou_unmatched_gt": histogram(best_unmatched, iou_edges),
        },
        "iou_quantiles_matched": quantiles(matched_iou),
        "complex_roofs": complex_roof_stats(ev, float(ea["convexity_threshold"])),
        "tile_border": tile_border_stats(ev, float(ea["tile_border_buffer_m"])),
        "density": density_stats(ev, per_tile),
        "raster_proxies": raster_proxies(
            ev,
            tiles_dir,
            float(ea["shadow_luminance_quantile"]),
            float(ea["vegetation_exg_threshold"]),
        ),
        "notes": list(ev.notes),
    }


def _f(x: Any, nd: int = 3) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, int | np.integer):
        return str(int(x))
    return f"{float(x):.{nd}f}"


def _pct(x: Any) -> str:
    return "—" if x is None else f"{100 * float(x):.1f} %"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out.extend("| " + " | ".join(str(c) for c in row) + " |" for row in rows)
    return "\n".join(out)


def build_markdown(
    a: dict[str, Any],
    figures: dict[str, str],
    tile_figures: dict[str, str],
    sources: dict[str, str],
    header: dict[str, Any],
) -> str:
    s = a["summary"]
    parts: list[str] = [
        f"# Анализ ошибок детекции — split `{a['split']}`",
        "",
        f"Сгенерировано `geoqc eval error-analysis` {header['generated_at']}, commit `{header.get('git_commit') or 'n/a'}`. "
        f"Все цифры — из `{sources['json']}` (та же оценка, что в `{sources['metrics']}`); IoU-порог {_f(a['iou_threshold'], 2)}, "
        f"сопоставление `{a['match_method']}`."
        + (
            " **Не метрика: разметка не верифицирована (smoke-прогон).**"
            if not header.get("is_metric", True)
            else ""
        ),
        "",
        "## Сводка",
        "",
        _table(
            ["TP", "FP", "FN", "Precision", "Recall", "F1", "mean IoU (TP)"],
            [
                [
                    s["tp"],
                    s["fp"],
                    s["fn"],
                    _f(s["precision"]),
                    _f(s["recall"]),
                    _f(s["f1"]),
                    _f(s["mean_iou_matched"]),
                ]
            ],
        ),
        "",
        "## По стратам",
        "",
    ]
    if a["per_stratum"]:
        parts.append(
            _table(
                ["Страта", "Тайлов", "GT", "TP", "FP", "FN", "P", "R", "F1", "mean IoU"],
                [
                    [
                        k,
                        v["n_tiles"],
                        v["n_gt"],
                        v["tp"],
                        v["fp"],
                        v["fn"],
                        _f(v["precision"]),
                        _f(v["recall"]),
                        _f(v["f1"]),
                        _f(v["mean_iou_matched"]),
                    ]
                    for k, v in a["per_stratum"].items()
                ],
            )
        )
    else:
        parts.append("Нет данных: тайлы сплита не найдены.")
    parts += ["", f"## Топ-{len(a['worst_tiles'])} худших тайлов (по F1)", ""]
    if a["worst_tiles"]:
        parts.append(
            _table(
                ["tile_id", "Страта", "GT", "det", "TP", "FP", "FN", "F1", "mean IoU", "Рисунок"],
                [
                    [
                        t["tile_id"],
                        t["stratum"],
                        t["n_gt"],
                        t["n_det"],
                        t["tp"],
                        t["fp"],
                        t["fn"],
                        _f(t["f1"]),
                        _f(t["mean_iou_matched"]),
                        f"`{tile_figures[t['tile_id']]}`" if t["tile_id"] in tile_figures else "—",
                    ]
                    for t in a["worst_tiles"]
                ],
            )
        )
    else:
        parts.append("Нет данных.")
    bins = a["area_bins_m2"]
    bin_labels = [
        f"{int(lo)}–{int(hi)}" if hi < 1e5 else f"≥ {int(lo)}"
        for lo, hi in zip(bins[:-1], bins[1:], strict=True)
    ]
    parts += [
        "",
        "## Распределение площадей ошибок (м²)",
        "",
        _table(
            ["Площадь, м²", *bin_labels],
            [
                ["FP", *a["area_histogram"]["fp"]["counts"]],
                ["FN", *a["area_histogram"]["fn"]["counts"]],
                ["TP (GT)", *a["area_histogram"]["tp_gt"]["counts"]],
            ],
        ),
        "",
        _table(
            ["", "n", "min", "p25", "медиана", "p75", "max", "среднее"],
            [
                [
                    k,
                    q["n"],
                    _f(q["min"], 1),
                    _f(q["p25"], 1),
                    _f(q["median"], 1),
                    _f(q["p75"], 1),
                    _f(q["max"], 1),
                    _f(q["mean"], 1),
                ]
                for k, q in a["area_quantiles"].items()
            ],
        ),
        "",
        f"Рисунок: `{figures.get('area_hist', '—')}`",
        "",
        "## Гистограмма IoU сопоставленных пар",
        "",
    ]
    ih = a["iou_histogram"]["matched"]
    bu = a["iou_histogram"]["best_iou_unmatched_gt"]
    edges = ih["edges"]
    parts += [
        _table(
            ["IoU", *[f"{lo:.1f}–{hi:.1f}" for lo, hi in zip(edges[:-1], edges[1:], strict=True)]],
            [["пары TP", *ih["counts"]], ["лучший IoU FN-объектов", *bu["counts"]]],
        ),
        "",
        f"Квантили IoU пар TP: медиана {_f(a['iou_quantiles_matched']['median'])}, p25 {_f(a['iou_quantiles_matched']['p25'])}, p75 {_f(a['iou_quantiles_matched']['p75'])} (n = {a['iou_quantiles_matched']['n']}). "
        f"Рисунок: `{figures.get('iou_hist', '—')}`",
        "",
    ]
    # ---- five named sections -------------------------------------------------
    rp = a["raster_proxies"]
    parts += ["## Тени", ""]
    if rp is None:
        parts.append(
            "Нет данных: растры тайлов (`data/tiles/<tile_id>.tif`) недоступны, яркостный прокси не вычислен."
        )
    else:
        sh = rp["shadow"]
        parts.append(
            f"Прокси: средняя яркость пикселей (0.299R + 0.587G + 0.114B) внутри контура объекта. "
            f"Медиана яркости TP-объектов GT {_f(sh['median_luminance_tp'], 1)}, FN-объектов {_f(sh['median_luminance_fn'], 1)}. "
            f"Порог «тёмный» = квантиль {sh['luminance_quantile']} яркости TP = {_f(sh['threshold_luminance'], 1)}. "
            f"FN темнее порога: {sh['n_fn_dark']} из {sh['n_fn']} ({_pct(sh['share_fn_dark'])}). Растров: {rp['n_rasters']}."
        )
    cr = a["complex_roofs"]
    parts += ["", "## Сложные крыши", ""]
    if cr.get("n_gt", 0) == 0:
        parts.append("Нет данных: нет объектов GT.")
    else:
        parts.append(
            f"Прокси формы: выпуклость = area / area(convex hull); «сложный» контур при выпуклости < {cr['threshold']}. "
            f"Сложных контуров {cr['complex']['n']}, FN среди них {cr['complex']['fn']} ({_pct(cr['complex']['fn_rate'])}), mean IoU {_f(cr['complex']['mean_iou_matched'])}; "
            f"простых {cr['simple']['n']}, FN {cr['simple']['fn']} ({_pct(cr['simple']['fn_rate'])}), mean IoU {_f(cr['simple']['mean_iou_matched'])}. "
            f"Медиана выпуклости GT {_f(cr['convexity_median_all'])}."
        )
    de = a["density"]
    parts += ["", "## Плотная застройка", ""]
    if de.get("dense_stratum"):
        d = de["dense_stratum"]
        parts.append(
            f"Страта `dense_multistorey`: тайлов {d['n_tiles']}, GT {d['n_gt']}, TP {d['tp']}, FP {d['fp']}, FN {d['fn']}, P {_f(d['precision'])}, R {_f(d['recall'])}, F1 {_f(d['f1'])}, mean IoU {_f(d['mean_iou_matched'])}."
        )
    else:
        parts.append("Страта `dense_multistorey` в сплите отсутствует.")
    if "densest_quartile" in de:
        dq, rest = de["densest_quartile"], de["rest"]
        unit = (
            "объектов GT на га"
            if de["density_metric"] == "density_per_ha"
            else "объектов GT на тайл"
        )
        parts.append(
            f"Плотность ({unit}), порог верхнего квартиля {_f(de['q75_threshold'], 1)}: самые плотные тайлы ({dq['n_tiles']}: {', '.join(de['densest_tiles'])}) — "
            f"P {_f(dq['precision'])}, R {_f(dq['recall'])}, F1 {_f(dq['f1'])} (TP {dq['tp']}, FP {dq['fp']}, FN {dq['fn']}); остальные ({rest['n_tiles']}) — P {_f(rest['precision'])}, R {_f(rest['recall'])}, F1 {_f(rest['f1'])}."
        )
    elif de.get("note"):
        parts.append(f"Нет данных по плотности: {de['note']}.")
    parts += ["", "## Растительность", ""]
    if rp is None:
        parts.append("Нет данных: растры тайлов недоступны, индекс ExG не вычислен.")
    else:
        vg = rp["vegetation"]
        parts.append(
            f"Прокси: средний ExG = 2G − R − B внутри контура детекции; «зелёный» при ExG > {_f(vg['exg_threshold'], 0)}. "
            f"Медиана ExG TP-детекций {_f(vg['median_exg_tp'], 1)}, FP-детекций {_f(vg['median_exg_fp'], 1)}. "
            f"Зелёных FP: {vg['n_fp_green']} из {vg['n_fp']} ({_pct(vg['share_fp_green'])}); среди TP зелёных {_pct(vg['share_tp_green'])}."
        )
    tb = a["tile_border"]
    parts += ["", "## Граница тайла", ""]
    if tb is None:
        parts.append("Нет данных: полигоны тайлов (`data/tiles/tiles.gpkg`) недоступны.")
    else:
        parts.append(
            f"Объект «на границе», если расстояние до края своего тайла ≤ {tb['buffer_m']} м. "
            f"FN на границе: {tb['fn']['at_border']} из {tb['fn']['n_with_tile']} ({_pct(tb['fn']['share'])}); FP на границе: {tb['fp']['at_border']} из {tb['fp']['n_with_tile']} ({_pct(tb['fp']['share'])}). "
            f"Для сравнения TP: GT {_pct(tb['tp_gt']['share'])}, детекции {_pct(tb['tp_det']['share'])}."
        )
    parts += ["", "## Примеры (рисунки)", ""]
    if tile_figures:
        parts.extend(f"- `{tid}` → `{path}`" for tid, path in tile_figures.items())
    else:
        parts.append("Рисунки не созданы (нет тайлов).")
    if a["notes"]:
        parts += ["", "## Замечания расчёта", ""] + [f"- {n}" for n in a["notes"]]
    return "\n".join(parts) + "\n"


def run_error_analysis(
    ev: SplitEvaluation,
    paths: EvalPaths,
    ecfg: dict[str, Any],
    *,
    out_md: Path | None = None,
    out_json: Path | None = None,
    figures_dir: Path | None = None,
    render: bool = True,
) -> tuple[dict[str, Any], Path]:
    """Compute the analysis, render figures and write markdown + json; returns (analysis, md path)."""
    figures_dir = Path(figures_dir) if figures_dir else paths.figures_dir
    out_md = Path(out_md) if out_md else paths.reports_dir / "error_analysis.md"
    out_json = Path(out_json) if out_json else out_md.with_suffix(".json")
    analysis = analyse(ev, ecfg, paths.tiles_dir)
    figures: dict[str, str] = {}
    tile_figures: dict[str, str] = {}
    if render:
        figures = {
            k: rel(Path(v), paths.repo_root)
            for k, v in render_histograms(analysis, figures_dir, ev.split).items()
        }
        for t in analysis["worst_tiles"][: int(ecfg["error_analysis"]["n_example_figures"])]:
            tid = str(t["tile_id"])
            png = render_tile_figure(
                ev, tid, paths.tiles_dir, figures_dir / f"error_{ev.split}_{tid}.png"
            )
            if png:
                tile_figures[tid] = rel(Path(png), paths.repo_root)
    header = {
        "generated_at": now_iso(),
        "git_commit": read_git_commit(paths.repo_root),
        "is_metric": bool(ev.labels_verified),
    }
    analysis.update(
        {
            "generated_at": header["generated_at"],
            "git_commit": header["git_commit"],
            "is_metric": header["is_metric"],
            "figures": figures,
            "tile_figures": tile_figures,
        }
    )
    write_json(out_json, analysis)
    md = build_markdown(
        analysis,
        figures,
        tile_figures,
        {
            "json": rel(out_json, paths.repo_root),
            "metrics": rel(paths.metrics(ev.split, smoke=not ev.labels_verified), paths.repo_root),
        },
        header,
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md, encoding="utf-8")
    return analysis, out_md
