"""CLI `geoqc eval`: отказ без входов, одноразовость test, smoke-режим, полный синтетический прогон."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from typer.testing import CliRunner

from geoqc.eval.cli import app

runner = CliRunner()


def _run(env, *args):
    return runner.invoke(app, [*args, *env.cli_args])


def test_detection_without_inputs_fails_clearly(tmp_path, monkeypatch, synthetic_env):
    """Нет tiles_split.csv -> код 2 и подсказка, какой командой его получить."""
    empty = tmp_path / "empty"
    empty.mkdir()
    res = runner.invoke(
        app,
        ["detection", "--split", "val", "--labels-dir", str(empty), "--reports-dir", str(empty)],
    )
    assert res.exit_code == 2
    assert "tiles split not found" in res.output and "select-tiles" in res.output
    res = runner.invoke(app, ["detection", "--split", "train"])
    assert res.exit_code == 2


def test_gates_without_any_report(tmp_path, synthetic_env):
    """`gates` на пустом каталоге отчётов пишет metrics.json с null и таблицу FAIL по всем гейтам."""
    empty = tmp_path / "empty_reports"
    res = runner.invoke(app, ["gates", "--reports-dir", str(empty)])
    assert res.exit_code == 0, res.output
    metrics = json.loads((empty / "metrics.json").read_text())
    assert metrics["detection"]["val"]["f1"] is None
    assert all(not g["passed"] for g in metrics["gates"].values())
    assert "passed 0/9" in res.output


def test_detection_val_and_smoke_on_draft(synthetic_env):
    """val: метрика записана; draft без флага -> отказ; с --allow-unverified -> файл *_smoke.json и пометка NOT A METRIC."""
    env = synthetic_env
    res = _run(env, "detection", "--split", "val")
    assert res.exit_code == 0, res.output
    report = json.loads(env.paths.metrics("val").read_text())
    assert report["is_metric"] is True and report["detection"]["f1"] == pytest.approx(0.8)
    assert report["per_tile"][0]["tile_id"] == "0_0" and report["n_tiles"] == 2
    res = _run(env, "detection", "--split", "val", "--labels", str(env.draft_val))
    assert res.exit_code == 2 and "not all verified" in res.output
    res = _run(
        env, "detection", "--split", "val", "--labels", str(env.draft_val), "--allow-unverified"
    )
    assert res.exit_code == 0, res.output
    assert "NOT A METRIC" in res.output
    smoke = json.loads(env.paths.metrics("val", smoke=True).read_text())
    assert smoke["is_metric"] is False and smoke["labels_verified"] is False
    res = _run(
        env, "detection", "--split", "val", "--method", "hungarian", "--iou-threshold", "0.7"
    )
    assert res.exit_code == 0
    assert json.loads(env.paths.metrics("val").read_text())["match_method"] == "hungarian"


def test_detection_test_is_one_shot(synthetic_env):
    """Повторный `--split test` отказывает (код 3), --force без --reason тоже, --force --reason перезаписывает; всё в runs.log."""
    env = synthetic_env
    log = env.paths.test_runs_log
    assert _run(env, "detection", "--split", "test").exit_code == 0
    assert env.paths.metrics("test").exists()
    res = _run(env, "detection", "--split", "test")
    assert res.exit_code == 3 and "evaluated once" in res.output
    res = _run(env, "detection", "--split", "test", "--force")
    assert res.exit_code == 2 and "--reason" in res.output
    res = _run(env, "detection", "--split", "test", "--force", "--reason", "bugfix in clipping")
    assert res.exit_code == 0, res.output
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [ln.split()[1] for ln in lines] == [
        "outcome=completed",
        "outcome=refused",
        "outcome=refused",
        "outcome=forced",
    ]
    assert "bugfix in clipping" in lines[-1]
    smoke = _run(
        env, "detection", "--split", "test", "--labels", str(env.draft_val), "--allow-unverified"
    )
    assert smoke.exit_code == 0 and env.paths.metrics("test", smoke=True).exists()
    assert log.read_text(encoding="utf-8").splitlines()[-1].split()[1] == "outcome=smoke"


def test_full_synthetic_pipeline_through_cli(synthetic_env):
    """Полный цикл: sample -> вердикты -> discrepancies -> compare-secondary -> error-analysis -> gates (G3, G4, G5, G7 PASS)."""
    env = synthetic_env
    rd = env.paths.reports_dir
    assert _run(env, "detection", "--split", "val").exit_code == 0
    assert _run(env, "detection", "--split", "test").exit_code == 0

    res = runner.invoke(
        app,
        [
            "verification-sample",
            "--from-geojson",
            str(env.discrepancies_geojson),
            "--reports-dir",
            str(rd),
        ],
    )
    assert res.exit_code == 0, res.output
    sample = pd.read_csv(rd / "verification_sample.csv")
    assert len(sample) >= 50 and (sample["class"] == "missing_on_image").sum() == 15
    meta = json.loads((rd / "verification_sample.meta.json").read_text())
    assert meta["n_selected"] == len(sample)

    # the student marks the 20 most confident as map_error, then alternates
    sample = sample.sort_values("confidence", ascending=False).reset_index(drop=True)
    verdicts = ["map_error"] * 20 + ["model_error", "map_error", "unsure", "map_error"] * (
        (len(sample) - 20) // 4 + 1
    )
    sample["verdict"] = verdicts[: len(sample)]
    sample.to_csv(rd / "verification.csv", index=False)
    res = runner.invoke(
        app,
        [
            "discrepancies",
            "--verification",
            str(rd / "verification.csv"),
            "--from-geojson",
            str(env.discrepancies_geojson),
            "--reports-dir",
            str(rd),
        ],
    )
    assert res.exit_code == 0, res.output
    disc = json.loads((rd / "metrics_discrepancies.json").read_text())
    assert (
        disc["precision_at_n"]["20"]["value"] == 1.0
        and disc["counts_by_class"]["missing_in_map"] == 45
    )
    assert disc["n_checked"] == len(sample) and disc["n_not_in_queue"] == 0

    def run_stale(df: pd.DataFrame) -> dict:
        df.to_csv(rd / "verification_stale.csv", index=False)
        res = runner.invoke(
            app,
            [
                "discrepancies",
                "--verification",
                str(rd / "verification_stale.csv"),
                "--from-geojson",
                str(env.discrepancies_geojson),
                "--out",
                str(rd / "stale.json"),
                "--reports-dir",
                str(rd),
            ],
        )
        assert res.exit_code == 0, res.output
        return json.loads((rd / "stale.json").read_text())

    # the queue was rebuilt with new ids: the same discrepancies are found by class and point
    rebuilt = run_stale(sample.assign(id=sample["id"] + 10_000))
    assert rebuilt["n_not_in_queue"] == 0 and "re-anchored" in rebuilt["notes"][0]
    fixed = pd.read_csv(rd / "verification_stale.csv")
    assert fixed["id"].tolist() == sample["id"].tolist()
    assert fixed["verdict"].tolist() == sample["verdict"].tolist()
    # a different queue (other points): nothing to re-anchor, the sample is reported as stale
    other = run_stale(sample.assign(id=sample["id"] + 10_000, lon=sample["lon"] + 0.01))
    assert (
        other["n_not_in_queue"] == len(sample) and "not in the current queue" in other["notes"][0]
    )

    res = _run(
        env, "compare-secondary", "--split", "test", "--secondary", str(env.secondary_geojson)
    )
    assert res.exit_code == 0, res.output
    assert json.loads((rd / "comparison.json").read_text())["secondary_vs_gt"]["precision"] == 0.5

    res = _run(env, "error-analysis", "--split", "val")
    assert res.exit_code == 0, res.output
    assert (rd / "error_analysis.md").exists() and (rd / "error_analysis.json").exists()

    res = runner.invoke(
        app, ["gates", "--reports-dir", str(rd), "--tests-passed", "--n-tests", "10"]
    )
    assert res.exit_code == 0, res.output
    metrics = json.loads((rd / "metrics.json").read_text())
    gates = metrics["gates"]
    assert (
        gates["G3"]["passed"]
        and gates["G4"]["passed"]
        and gates["G5"]["passed"]
        and gates["G7"]["passed"]
        and gates["G9"]["passed"]
    )
    assert not gates["G1"]["passed"] and not gates["G2"]["passed"] and not gates["G8"]["passed"]
    assert metrics["dataset"]["n_tiles_val"] == 2 and metrics["dataset"]["n_gt_test"] == 1
    assert metrics["comparison"]["secondary_source"] == "microsoft"
    assert metrics["discrepancies"]["precision_at_n"] == {
        "n": 20,
        "value": 1.0,
        "n_available": 20,
        "value_incl_unsure": 1.0,
    }
