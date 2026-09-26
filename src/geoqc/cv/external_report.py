"""``geoqc cv report-external``: statistics and figures of the prepared SpaceNet sample vs our AOI.

Reads the output of ``prepare_spacenet`` (``split.csv``, ``buildings.gpkg``, ``images/``,
``labels/``) and, for comparison, our val tiles with OSM outlines (never labels of any split).
Writes ``reports/external_spacenet2.json`` and figures to ``docs/datasets/figures/``:

* ``spacenet2_samples.jpg`` — random chips per city with the reference outlines;
* ``spacenet2_vs_aoi.jpg`` — our tiles (OSM outlines) next to SpaceNet chips, same 0.3 m scale;
* ``spacenet2_cases.jpg`` — automatically picked hard cases (densest, smallest, empty, cut, nodata);
* ``spacenet2_stats.png`` — building area, buildings per tile, built-up share, vertices;
* ``spacenet2_color.png`` — per-channel brightness of SpaceNet chips vs our tiles.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import cv2
import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from shapely.geometry import box  # noqa: E402

from geoqc.config import repo_relative, resolve_path  # noqa: E402
from geoqc.cv.external import external_config  # noqa: E402

OUTLINE_REF = (255, 230, 0)
OUTLINE_OSM = (0, 220, 255)
CITY_RU = {
    "AOI_2_Vegas": "Лас-Вегас",
    "AOI_3_Paris": "Париж",
    "AOI_4_Shanghai": "Шанхай",
    "AOI_5_Khartoum": "Хартум",
    "AOI": "Пушкин (OSM)",
}


# ---------------------------------------------------------------- loading


def load_prepared(out_dir: Path) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    split = pd.read_csv(out_dir / "split.csv", dtype={"tile_id": str})
    gpkg = out_dir / "buildings.gpkg"
    b = gpd.read_file(gpkg) if gpkg.exists() else gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    return split, b


def chip_image(out_dir: Path, subset: str, tile_id: str) -> np.ndarray:
    img = cv2.imread(str(out_dir / "images" / subset / f"{tile_id}.png"))
    return img[:, :, ::-1].copy()


def chip_with_outlines(out_dir: Path, subset: str, tile_id: str) -> np.ndarray:
    return draw_yolo(
        chip_image(out_dir, subset, tile_id), out_dir / "labels" / subset / f"{tile_id}.txt"
    )


def draw_yolo(img: np.ndarray, label_path: Path, color=OUTLINE_REF, width: int = 1) -> np.ndarray:
    h, w = img.shape[:2]
    if label_path.exists():
        for line in label_path.read_text(encoding="utf-8").splitlines():
            vals = line.split()[1:]
            if len(vals) < 6:
                continue
            pts = (np.asarray(vals, float).reshape(-1, 2) * [w, h]).round().astype(np.int32)
            cv2.polylines(img, [pts], True, color, width, lineType=cv2.LINE_AA)
    return img


def draw_geoms(img: np.ndarray, geoms, transform, color=OUTLINE_OSM, width: int = 1) -> np.ndarray:
    inv = ~transform
    for g in geoms:
        parts = list(g.geoms) if hasattr(g, "geoms") else [g]
        for p in parts:
            if p.is_empty or p.geom_type != "Polygon":
                continue
            xs, ys = np.asarray(p.exterior.coords).T
            cols, rows = inv @ (xs, ys)
            pts = np.stack([cols, rows], axis=1).round().astype(np.int32)
            cv2.polylines(img, [pts], True, color, width, lineType=cv2.LINE_AA)
    return img


def aoi_samples(cfg: dict[str, Any], n: int, seed: int) -> list[tuple[str, np.ndarray, int]]:
    """``n`` val tiles of our AOI (one per stratum first) with OSM outlines: (title, image, n_osm)."""
    from geoqc.cv.tiles import read_tile

    paths = cfg.get("paths", {})
    split_csv = Path(resolve_path(Path(paths.get("labels", "data/labels")) / "tiles_split.csv"))
    osm_path = Path(
        resolve_path(Path(paths.get("interim", "data/interim")) / "osm_buildings.geojson")
    )
    tiles_dir = Path(resolve_path(paths.get("tiles", "data/tiles")))
    if not split_csv.exists() or not osm_path.exists():
        return []
    split = pd.read_csv(split_csv, dtype={"tile_id": str})
    val = split[split["split"] == "val"].sort_values("tile_id")
    rng = random.Random(seed)
    picked = []
    for _, g in val.groupby("stratum"):
        picked.append(g.iloc[rng.randrange(len(g))])
    rng.shuffle(picked)
    rest = [r for _, r in val.iterrows() if r.tile_id not in {p.tile_id for p in picked}]
    rng.shuffle(rest)
    picked += rest
    osm = gpd.read_file(osm_path)
    out = []
    for row in picked[:n]:
        tile = read_tile(tiles_dir / f"{row.tile_id}.tif", row.tile_id)
        h, w = tile.shape
        x0, y0 = tile.transform * (0, 0)
        x1, y1 = tile.transform * (w, h)
        frame = box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        o = osm.to_crs(tile.crs)
        o = o[o.intersects(frame)]
        img = draw_geoms(
            tile.image.copy(), [g.intersection(frame) for g in o.geometry], tile.transform
        )
        out.append((f"Пушкин · {row.stratum} · OSM {len(o)}", img, len(o)))
    return out


# ---------------------------------------------------------------- statistics


def _touching_share(gdf_m: gpd.GeoDataFrame, gap_m: float = 0.5) -> float:
    """Share of buildings that touch another one within ``gap_m`` (attached / terraced houses)."""
    if len(gdf_m) < 2:
        return 0.0
    buf = gdf_m[["geometry"]].copy()
    buf["geometry"] = buf.geometry.buffer(gap_m / 2)
    j = gpd.sjoin(buf, buf, predicate="intersects")
    j = j[j.index != j["index_right"]]
    return float(j.index.nunique() / len(gdf_m))


def geometry_stats(gdf_m: gpd.GeoDataFrame) -> dict[str, Any]:
    """Area / shape statistics of polygons in a metric CRS."""
    if len(gdf_m) == 0:
        return {"n": 0}
    area = gdf_m.geometry.area
    parts = [
        max(g.geoms, key=lambda p: p.area) if hasattr(g, "geoms") else g for g in gdf_m.geometry
    ]
    verts = np.array([len(p.exterior.coords) - 1 for p in parts])
    holes = np.array([len(p.interiors) for p in parts])
    rect = np.array([p.area / max(p.minimum_rotated_rectangle.area, 1e-9) for p in parts])
    q = np.percentile(area, [10, 25, 50, 75, 90])
    return {
        "n": int(len(gdf_m)),
        "area_m2": {
            f"p{k}": round(float(v), 1) for k, v in zip((10, 25, 50, 75, 90), q, strict=True)
        },
        "share_small_lt_50m2": round(float((area < 50).mean()), 3),
        "share_large_gt_500m2": round(float((area > 500).mean()), 3),
        "vertices_median": float(np.median(verts)),
        "share_with_holes": round(float((holes > 0).mean()), 3),
        "rectangularity_median": round(float(np.median(rect)), 3),
        "share_touching": round(_touching_share(gdf_m), 3),
    }


def spacenet_stats(split: pd.DataFrame, b: gpd.GeoDataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for city, s in split.groupby("city"):
        parts = []
        for crs, chips in s.groupby("crs"):
            sel = b[b["chip_id"].isin(chips["tile_id"])]
            if len(sel):
                parts.append(sel.to_crs(crs))
        gm = pd.concat(parts) if parts else gpd.GeoDataFrame(geometry=[])
        out[city] = {
            "chips": int(len(s)),
            "share_empty_chips": round(float((s["n_instances"] == 0).mean()), 3),
            "buildings_per_chip_mean": round(float(s["n_instances"].mean()), 1),
            "buildings_per_chip_p90": float(np.percentile(s["n_instances"], 90)),
            "coverage_mean": round(float(s["coverage"].mean()), 3),
            "share_partial": round(float(s["n_partial"].sum() / max(s["n_instances"].sum(), 1)), 3),
            "valid_fraction_mean": round(float(s["valid_fraction"].mean()), 3),
            **geometry_stats(gm),
        }
    return out


def aoi_stats(cfg: dict[str, Any]) -> tuple[dict[str, Any], gpd.GeoDataFrame, pd.Series]:
    """OSM buildings inside our AOI and per-tile counts over all AOI tiles (centroid in tile; tiles overlap)."""
    paths = cfg.get("paths", {})
    osm_path = Path(
        resolve_path(Path(paths.get("interim", "data/interim")) / "osm_buildings.geojson")
    )
    aoi_path = Path(resolve_path("config/aoi.geojson"))
    index_path = Path(resolve_path(Path(paths.get("tiles", "data/tiles")) / "tiles.gpkg"))
    metric = (cfg.get("crs") or {}).get("metric")
    if not (osm_path.exists() and aoi_path.exists() and metric):
        return {}, gpd.GeoDataFrame(geometry=[]), pd.Series(dtype=float)
    aoi = gpd.read_file(aoi_path).to_crs(metric).union_all()
    osm = gpd.read_file(osm_path).to_crs(metric)
    osm = osm[osm.intersects(aoi)]
    counts = pd.Series(dtype=float)
    if index_path.exists():
        tiles = gpd.read_file(index_path).to_crs(metric)
        j = gpd.sjoin(osm[["geometry"]].set_geometry(osm.centroid), tiles[["tile_id", "geometry"]])
        counts = j.groupby("tile_id").size().reindex(tiles["tile_id"], fill_value=0)
    stats = {
        "buildings_per_tile_mean": round(float(counts.mean()), 1) if len(counts) else None,
        "share_empty_tiles": round(float((counts == 0).mean()), 3) if len(counts) else None,
        **geometry_stats(osm),
    }
    return stats, osm, counts


def color_stats(images: list[np.ndarray]) -> dict[str, list[float]]:
    px = np.concatenate([im.reshape(-1, 3)[im.reshape(-1, 3).any(axis=1)] for im in images])
    return {"mean": px.mean(axis=0).round(1).tolist(), "std": px.std(axis=0).round(1).tolist()}


# ---------------------------------------------------------------- figures


def _grid(panels: list[tuple[str, np.ndarray]], ncols: int, path: Path, suptitle: str) -> None:
    nrows = int(np.ceil(len(panels) / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(3.4 * ncols, 3.9 * nrows), squeeze=False)
    for ax in axes.ravel():
        ax.axis("off")
    for ax, (title, img) in zip(axes.ravel(), panels, strict=False):
        ax.imshow(img)
        ax.set_title(title, fontsize=8)
    fig.suptitle(suptitle, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=110, pil_kwargs={"quality": 85} if path.suffix == ".jpg" else None)
    plt.close(fig)


def build_report(
    cv_cfg: dict[str, Any],
    pipeline_cfg: dict[str, Any],
    *,
    out_dir: str | Path | None = None,
    figures_dir: str | Path = "docs/datasets/figures",
    report_path: str | Path = "reports/external_spacenet2.json",
    per_city: int = 4,
    seed: int = 7,
) -> dict[str, Any]:
    ext = external_config(cv_cfg)
    data_dir = Path(resolve_path(out_dir or ext["out_dir"]))
    figs = Path(resolve_path(figures_dir))
    figs.mkdir(parents=True, exist_ok=True)
    split, b = load_prepared(data_dir)
    rng = random.Random(seed)

    # samples per city (non-empty chips so outlines are visible)
    panels = []
    for city, s in sorted(split.groupby("city")):
        rows = list(s[s["n_instances"] > 0].itertuples(index=False))
        for r in rng.sample(rows, min(per_city, len(rows))):
            img = chip_with_outlines(data_dir, r.subset, r.tile_id)
            panels.append((f"{CITY_RU.get(city, city)} · {r.n_instances} зд.", img))
    _grid(
        panels,
        per_city,
        figs / "spacenet2_samples.jpg",
        "SpaceNet 2: примеры тайлов 512×512, 0,3 м, жёлтый — эталонные контуры",
    )

    # our AOI vs SpaceNet
    ours = aoi_samples(pipeline_cfg, per_city, seed)
    sn_rows = list(split[split["n_instances"] > 0].itertuples(index=False))
    sn = [
        (
            f"{CITY_RU.get(r.city, r.city)} · {r.n_instances} зд.",
            chip_with_outlines(data_dir, r.subset, r.tile_id),
        )
        for r in rng.sample(sn_rows, min(per_city, len(sn_rows)))
    ]
    if ours:
        _grid(
            [(t, im) for t, im, _ in ours] + sn,
            per_city,
            figs / "spacenet2_vs_aoi.jpg",
            "Сверху — наш снимок (голубой — OSM), снизу — SpaceNet (жёлтый — эталон); масштаб одинаковый",
        )

    # hard cases picked by rule
    cases: list[tuple[str, Any]] = []
    nonempty = split[split["n_instances"] > 0]
    if len(nonempty):
        cases.append(("Самая плотная застройка", nonempty.loc[nonempty["n_instances"].idxmax()]))
        med = b.groupby("chip_id")["area_m2"].median() if len(b) else pd.Series(dtype=float)
        med = med[med.index.isin(nonempty[nonempty["n_instances"] >= 5]["tile_id"])]
        if len(med):
            cid = med.idxmin()
            cases.append(("Мелкие постройки", split[split["tile_id"] == cid].iloc[0]))
        cases.append(("Здания, обрезанные краем", nonempty.loc[nonempty["n_partial"].idxmax()]))
        cases.append(("Макс. застроенная площадь", nonempty.loc[nonempty["coverage"].idxmax()]))
    empty = split[split["n_instances"] == 0]
    if len(empty):
        cases.append(("Пустой тайл (негатив)", empty.iloc[rng.randrange(len(empty))]))
    cases.append(("Край снимка (nodata)", split.loc[split["valid_fraction"].idxmin()]))
    case_panels = []
    for title, r in cases:
        img = chip_with_outlines(data_dir, r["subset"], r["tile_id"])
        case_panels.append(
            (f"{title}\n{CITY_RU.get(r['city'], r['city'])} · {int(r['n_instances'])} зд.", img)
        )
    _grid(case_panels, 3, figs / "spacenet2_cases.jpg", "SpaceNet 2: характерные трудные случаи")

    # statistics
    sn_stats = spacenet_stats(split, b)
    aoi, osm_m, aoi_counts = aoi_stats(pipeline_cfg)
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    bins = np.logspace(np.log10(10), np.log10(5000), 40)
    for city, s in sorted(split.groupby("city")):
        sel = b[b["chip_id"].isin(s["tile_id"])]
        label = CITY_RU.get(city, city)
        axes[0, 0].hist(sel["area_m2"], bins=bins, histtype="step", density=True, label=label)
        axes[0, 1].hist(s["n_instances"], bins=30, histtype="step", density=True, label=label)
        axes[1, 0].hist(
            s["coverage"], bins=np.linspace(0, 0.8, 30), histtype="step", density=True, label=label
        )
        axes[1, 1].hist(
            sel["n_vertices"].clip(upper=40),
            bins=np.arange(3, 42),
            histtype="step",
            density=True,
            label=label,
        )
    if len(osm_m):
        axes[0, 0].hist(
            osm_m.geometry.area,
            bins=bins,
            histtype="stepfilled",
            alpha=0.25,
            density=True,
            color="k",
            label=CITY_RU["AOI"],
        )
        axes[0, 1].hist(
            aoi_counts,
            bins=30,
            histtype="stepfilled",
            alpha=0.25,
            density=True,
            color="k",
            label=CITY_RU["AOI"],
        )
        parts = [
            max(g.geoms, key=lambda p: p.area) if hasattr(g, "geoms") else g for g in osm_m.geometry
        ]
        v = np.clip([len(p.exterior.coords) - 1 for p in parts], None, 40)
        axes[1, 1].hist(
            v,
            bins=np.arange(3, 42),
            histtype="stepfilled",
            alpha=0.25,
            density=True,
            color="k",
            label=CITY_RU["AOI"],
        )
    axes[0, 0].set_xscale("log")
    axes[0, 0].set(title="Площадь здания, м² (лог. шкала)", xlabel="м²")
    axes[0, 1].set(title="Зданий на тайл 512×512", xlabel="шт.")
    axes[1, 0].set(title="Доля застроенной площади тайла", xlabel="доля")
    axes[1, 1].set(title="Вершин в контуре", xlabel="шт. (≥40 в последнем столбце)")
    for ax in axes.ravel():
        ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(figs / "spacenet2_stats.png", dpi=110)
    plt.close(fig)

    # colour
    sn_imgs = {
        city: [chip_image(data_dir, r.subset, r.tile_id) for r in s.head(60).itertuples()]
        for city, s in sorted(split.groupby("city"))
    }
    our_imgs = [im for _, im, _ in aoi_samples(pipeline_cfg, 40, seed)] if ours else []
    colors = {city: color_stats(ims) for city, ims in sn_imgs.items() if ims}
    if our_imgs:
        colors["AOI"] = color_stats(our_imgs)
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.4))
    for k, name in enumerate(("R", "G", "B")):
        for key, ims in [*sn_imgs.items(), ("AOI", our_imgs)]:
            if not ims:
                continue
            px = np.concatenate([im.reshape(-1, 3)[im.reshape(-1, 3).any(axis=1), k] for im in ims])
            axes[k].hist(
                px,
                bins=64,
                range=(0, 255),
                histtype="step",
                density=True,
                label=CITY_RU.get(key, key),
            )
        axes[k].set(title=f"Канал {name}", xlabel="яркость 0–255")
        axes[k].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(figs / "spacenet2_color.png", dpi=110)
    plt.close(fig)

    report = {
        "dataset_dir": str(data_dir),
        "meta": json.loads((data_dir / "meta.json").read_text(encoding="utf-8")),
        "spacenet": sn_stats,
        "aoi_osm": aoi,
        "color": colors,
        "figures": sorted(str(p) for p in figs.glob("spacenet2_*")),
        "note": "descriptive statistics of the training material, not model metrics",
    }
    rp = Path(resolve_path(report_path))
    rp.parent.mkdir(parents=True, exist_ok=True)
    report = repo_relative(report)
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
