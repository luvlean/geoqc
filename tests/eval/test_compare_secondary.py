"""Сравнение детекций и второго источника против ручной разметки test."""

from __future__ import annotations

import pytest

from geoqc.eval.compare_secondary import compare


def test_compare_secondary_all_three_blocks(synthetic_env):
    """test: детекции P=R=F1=1; secondary как детектор P=0.5 R=1; детекции против secondary как эталона R=0.5."""
    res = compare("test", synthetic_env.paths, synthetic_env.cfg)
    assert res["secondary_source"] == "microsoft" and res["is_metric"] is True
    assert res["detection_vs_gt"]["f1"] == pytest.approx(1.0)
    sec = res["secondary_vs_gt"]
    assert (sec["tp"], sec["fp"], sec["fn"]) == (1, 1, 0) and sec[
        "mean_iou_matched"
    ] == pytest.approx(380 / 420)
    dvs = res["detection_vs_secondary"]
    assert (dvs["tp"], dvs["fp"], dvs["fn"]) == (1, 0, 1) and dvs["recall"] == pytest.approx(0.5)
    assert res["n_secondary_in_tiles"] == 2
    assert res["n_gt_copied_from_secondary"] == 0 and not any("biased" in n for n in res["notes"])


def test_compare_secondary_flags_unedited_drafts(synthetic_env):
    """Разметка совпадает со вторым источником (черновики не правились) -> счётчик копий и предупреждение."""
    labels = synthetic_env.paths.labels("test")
    res = compare("test", synthetic_env.paths, synthetic_env.cfg, secondary_path=labels)
    assert res["secondary_vs_gt"]["f1"] == pytest.approx(1.0)
    assert res["n_gt_copied_from_secondary"] == res["secondary_vs_gt"]["tp"] > 0
    assert any("biased upward" in n for n in res["notes"])


def test_compare_secondary_missing_inputs_are_null_with_notes(synthetic_env):
    """Нет второго источника -> блоки null и note; неверифицированная разметка без флага -> сравнение с GT пропущено."""
    res = compare(
        "test",
        synthetic_env.paths,
        synthetic_env.cfg,
        secondary_path=synthetic_env.root / "nope.geojson",
    )
    assert res["secondary_vs_gt"] is None and res["detection_vs_secondary"] is None
    assert res["detection_vs_gt"]["tp"] == 1
    assert any("secondary footprints not found" in n for n in res["notes"])
    res = compare(
        "val", synthetic_env.paths, synthetic_env.cfg, labels_path=synthetic_env.draft_val
    )
    assert res["detection_vs_gt"] is None and res["is_metric"] is False
    assert any("not all verified" in n for n in res["notes"])
    res = compare(
        "val",
        synthetic_env.paths,
        synthetic_env.cfg,
        labels_path=synthetic_env.draft_val,
        allow_unverified=True,
    )
    assert res["detection_vs_gt"]["tp"] == 4 and res["is_metric"] is False
