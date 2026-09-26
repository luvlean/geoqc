"""Анализ ошибок: разделы markdown, прокси-статистики, рисунки."""

from __future__ import annotations

import json

import pytest
from shapely.geometry import Polygon, box

from geoqc.eval.common import load_eval_config
from geoqc.eval.detection_metrics import evaluate_split
from geoqc.eval.error_analysis import (
    SECTION_TITLES,
    complex_roof_stats,
    convexity,
    histogram,
    quantiles,
    run_error_analysis,
    tile_border_stats,
)


def test_convexity_histogram_quantiles():
    """Выпуклость квадрата 1, L-формы 256/328; гистограмма и квантили на простых данных и на пустом входе."""
    assert convexity(box(0, 0, 10, 10)) == pytest.approx(1.0)
    assert convexity(
        Polygon([(0, 0), (20, 0), (20, 8), (8, 8), (8, 20), (0, 20)])
    ) == pytest.approx(256 / 328)
    h = histogram([1, 5, 30, 300], [0, 10, 100, 1000])
    assert h["counts"] == [2, 1, 1] and h["n"] == 4
    assert histogram([], [0, 1])["counts"] == [0]
    q = quantiles([1, 2, 3, 4])
    assert q["median"] == 2.5 and q["min"] == 1 and q["max"] == 4
    assert quantiles([])["median"] is None


def test_complex_roofs_and_tile_border_on_synthetic(synthetic_env):
    """L-образный GT «сложный» и найден; FN у края тайла (3 м) учтён как граничный."""
    ev = evaluate_split("val", synthetic_env.paths, synthetic_env.cfg)
    cr = complex_roof_stats(ev, 0.85)
    assert (
        cr["complex"]["n"] == 1
        and cr["complex"]["fn"] == 0
        and cr["complex"]["mean_iou_matched"] == pytest.approx(0.64)
    )
    assert cr["simple"]["n"] == 4 and cr["simple"]["fn"] == 1
    tb = tile_border_stats(ev, 5.0)
    assert tb["fn"] == {"n": 1, "n_with_tile": 1, "at_border": 1, "share": 1.0}
    assert tb["fp"]["at_border"] == 0 and tb["fp"]["n"] == 1
    assert (
        tile_border_stats(evaluate_split("val", synthetic_env.paths, synthetic_env.cfg), 5.0)[
            "buffer_m"
        ]
        == 5.0
    )


def test_run_error_analysis_writes_markdown_json_and_figures(synthetic_env):
    """Markdown содержит все пять обязательных разделов и цифры; json и PNG созданы; растровые прокси посчитаны."""
    ev = evaluate_split("val", synthetic_env.paths, synthetic_env.cfg)
    analysis, md_path = run_error_analysis(
        ev, synthetic_env.paths, load_eval_config(synthetic_env.cfg)
    )
    text = md_path.read_text(encoding="utf-8")
    for title in SECTION_TITLES:
        assert f"## {title}" in text
    assert "| 4 | 1 | 1 |" in text  # TP FP FN row
    assert "Топ-2 худших тайлов" in text
    rp = analysis["raster_proxies"]
    assert rp["n_rasters"] == 2
    assert rp["shadow"]["n_fn_dark"] == 1 and rp["shadow"]["median_luminance_fn"] == pytest.approx(
        20.0
    )
    assert rp["vegetation"]["n_fp_green"] == 1 and rp["vegetation"]["share_tp_green"] == 0.0
    assert analysis["worst_tiles"][0]["tile_id"] == "0_1"  # F1 0.667 < 0.857
    figures_dir = synthetic_env.paths.figures_dir
    assert (figures_dir / "iou_hist_val.png").exists() and (
        figures_dir / "area_hist_val.png"
    ).exists()
    assert (figures_dir / "error_val_0_1.png").exists() and (
        figures_dir / "error_val_0_0.png"
    ).exists()
    payload = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["summary"]["f1"] == pytest.approx(0.8) and payload["is_metric"] is True
    assert payload["density"]["dense_stratum"]["n_tiles"] == 1


def test_run_error_analysis_without_rasters_or_tiles_says_no_data(synthetic_env):
    """Без растров разделы «Тени»/«Растительность» честно сообщают об отсутствии данных."""
    for tif in synthetic_env.paths.tiles_dir.glob("*.tif"):
        tif.unlink()
    ev = evaluate_split("val", synthetic_env.paths, synthetic_env.cfg)
    analysis, md_path = run_error_analysis(
        ev, synthetic_env.paths, load_eval_config(synthetic_env.cfg), render=False
    )
    text = md_path.read_text(encoding="utf-8")
    assert analysis["raster_proxies"] is None
    assert text.count("Нет данных") >= 2
    assert not (synthetic_env.paths.figures_dir / "iou_hist_val.png").exists()
