"""Гейты: операторы, отсутствующие метрики, полная схема metrics.json §3.9."""

from __future__ import annotations

import json

import pytest

from geoqc.eval.common import write_json
from geoqc.eval.gates import build_metrics, evaluate_gate, format_gates_table, get_by_path

# Ключи §3.9, переписаны из docs/CONTRACTS.md независимо от кода модуля.
SCHEMA_39 = {
    "schema_version": None,
    "generated_at": None,
    "git_commit": None,
    "dataset": {
        "aoi": None,
        "n_tiles_val": None,
        "n_tiles_test": None,
        "n_gt_val": None,
        "n_gt_test": None,
    },
    "model": {"model_version": None, "conf_threshold": None, "iou_threshold": None},
    "georeference": {"median_offset_m": None, "n_control_points": None},
    "annotation": {"agreement_iou": None, "agreement_f1": None, "n_checked": None},
    "detection": {
        "val": {
            "tp": None,
            "fp": None,
            "fn": None,
            "precision": None,
            "recall": None,
            "f1": None,
            "mean_iou_matched": None,
        },
        "test": {
            "tp": None,
            "fp": None,
            "fn": None,
            "precision": None,
            "recall": None,
            "f1": None,
            "mean_iou_matched": None,
        },
    },
    "discrepancies": {
        "counts_by_class": {
            "missing_in_map": None,
            "missing_on_image": None,
            "geometry_mismatch": None,
        },
        "verified_by_class": {
            "missing_in_map": {"n": None, "true_error": None, "precision": None},
            "missing_on_image": {"n": None, "true_error": None, "precision": None},
            "geometry_mismatch": {"n": None, "true_error": None, "precision": None},
        },
        "precision_overall": None,
        "precision_at_n": {"n": None, "value": None},
    },
    "comparison": {
        "secondary_source": None,
        "detection_vs_secondary": {"precision": None, "recall": None, "f1": None},
    },
    "reproducibility": {"make_all_passed": None},
    "tests": {"passed": None, "n_tests": None},
    "gates": {
        g: {"target": None, "op": None, "actual": None, "passed": None, "note": None}
        for g in [f"G{i}" for i in range(1, 10)]
    },
}


def _assert_keys(template, actual, path=""):
    for key, sub in template.items():
        assert key in actual, f"missing key {path}{key}"
        if isinstance(sub, dict):
            assert isinstance(actual[key], dict), f"{path}{key} must be an object"
            _assert_keys(sub, actual[key], f"{path}{key}.")


@pytest.mark.parametrize(
    ("op", "target", "actual", "passed"),
    [
        ("<=", 2.0, 1.5, True),
        ("<=", 2.0, 2.0, True),
        ("<=", 2.0, 2.1, False),
        (">=", 0.7, 0.7, True),
        (">=", 0.7, 0.69, False),
        ("==", True, True, True),
        ("==", True, False, False),
        (">", 1, 1, False),
        ("<", 1, 0, True),
    ],
)
def test_evaluate_gate_ops(op, target, actual, passed):
    """Операторы <=, >=, ==, <, > применяются как actual <op> target."""
    assert evaluate_gate(op, target, actual) == (passed, "")


def test_evaluate_gate_missing_or_bad():
    """Отсутствующая метрика или неизвестный оператор -> passed=False с пояснением."""
    assert evaluate_gate(">=", 0.7, None) == (False, "metric missing")
    passed, note = evaluate_gate("~", 1, 1)
    assert passed is False and "unknown op" in note
    passed, note = evaluate_gate(">=", 0.7, "abc")
    assert passed is False and "cannot compare" in note


def test_get_by_path():
    """Точечный путь по вложенному словарю; None для отсутствующих ключей."""
    data = {"a": {"b": {"c": 1}}}
    assert get_by_path(data, "a.b.c") == 1
    assert get_by_path(data, "a.x.c") is None
    assert get_by_path(data, "a.b.c.d") is None


def test_build_metrics_with_no_reports_has_full_schema_and_notes(synthetic_env):
    """Без единого отчёта metrics.json содержит все ключи §3.9, null-значения и passed=false с note у каждого гейта."""
    metrics = build_metrics(synthetic_env.paths, synthetic_env.cfg, git_commit="deadbeef")
    _assert_keys(SCHEMA_39, metrics)
    assert metrics["schema_version"] == "1.0" and metrics["git_commit"] == "deadbeef"
    assert (
        metrics["detection"]["val"]["f1"] is None
        and metrics["georeference"]["median_offset_m"] is None
    )
    assert metrics["discrepancies"]["precision_at_n"]["n"] == 20
    assert metrics["model"]["iou_threshold"] == 0.5
    for gid in [f"G{i}" for i in range(1, 10)]:
        assert metrics["gates"][gid]["passed"] is False
    for gid in ("G1", "G2", "G3", "G4", "G5", "G6", "G7"):
        assert "is null" in metrics["gates"][gid]["note"], gid
    assert "metrics_val.json" in metrics["gates"]["G3"]["note"]
    assert "georef.json" in metrics["gates"]["G1"]["note"]
    out = synthetic_env.paths.reports_dir / "metrics.json"
    write_json(out, metrics)
    assert json.loads(out.read_text())["gates"]["G8"]["actual"] is False


def test_build_metrics_merges_intermediate_reports(synthetic_env):
    """Промежуточные отчёты попадают в нужные блоки; smoke-отчёт test игнорируется с пометкой; флаги G8/G9 из аргументов."""
    rd = synthetic_env.paths.reports_dir
    det = {
        "tp": 8,
        "fp": 2,
        "fn": 2,
        "precision": 0.8,
        "recall": 0.8,
        "f1": 0.8,
        "mean_iou_matched": 0.71,
    }
    write_json(
        rd / "metrics_val.json",
        {
            "is_metric": True,
            "labels_verified": True,
            "n_tiles": 2,
            "n_gt": 10,
            "model_version": "m-1",
            "conf_threshold": 0.3,
            "detection": det,
        },
    )
    write_json(
        rd / "metrics_test.json",
        {"is_metric": False, "labels_verified": False, "n_tiles": 1, "n_gt": 1, "detection": det},
    )
    write_json(rd / "georef.json", {"median_offset_m": 1.2, "n_control_points": 12})
    write_json(
        rd / "annotation.json",
        {  # layout written by `geoqc annotation validate`
            "splits": {"val": {"agreement": {"agreement_iou": 0.7, "n_checked": 1}}},
            "annotation": {"agreement_iou": 0.85, "agreement_f1": 0.9, "n_checked": 3},
        },
    )
    write_json(
        rd / "metrics_discrepancies.json",
        {
            "counts_by_class": {
                "missing_in_map": 40,
                "missing_on_image": 10,
                "geometry_mismatch": 20,
            },
            "verified_by_class": {
                "missing_in_map": {"n": 20, "true_error": 15, "precision": 0.75, "unsure": 0}
            },
            "precision_overall": 0.72,
            "precision_at_n": {
                "20": {
                    "n": 20,
                    "n_available": 20,
                    "complete": True,
                    "value": 0.8,
                    "value_incl_unsure": 0.8,
                }
            },
            "n_checked": 50,
        },
    )
    write_json(
        rd / "comparison.json",
        {
            "secondary_source": "microsoft",
            "detection_vs_secondary": {"precision": 0.6, "recall": 0.5, "f1": 0.545},
        },
    )
    metrics = build_metrics(
        synthetic_env.paths, synthetic_env.cfg, make_all_passed=True, tests_passed=True, n_tests=42
    )
    _assert_keys(SCHEMA_39, metrics)
    gates = metrics["gates"]
    assert metrics["detection"]["val"] == det and gates["G3"]["passed"] is True
    assert (
        metrics["detection"]["test"]["f1"] is None
        and "smoke" in gates["G4"]["note"]
        and gates["G5"]["passed"] is False
    )
    assert gates["G1"]["passed"] is True and gates["G1"]["actual"] == 1.2
    assert gates["G2"]["passed"] is True and metrics["annotation"]["agreement_f1"] == 0.9
    assert gates["G6"]["passed"] is True and gates["G7"]["passed"] is True
    assert metrics["discrepancies"]["verified_by_class"]["missing_in_map"]["precision"] == 0.75
    assert metrics["discrepancies"]["verified_by_class"]["geometry_mismatch"]["n"] is None
    assert metrics["comparison"]["detection_vs_secondary"]["f1"] == 0.545
    assert (
        gates["G8"]["passed"] is True
        and gates["G9"]["passed"] is True
        and metrics["tests"]["n_tests"] == 42
    )
    assert metrics["dataset"]["n_gt_val"] == 10 and metrics["model"]["model_version"] == "m-1"
    table = format_gates_table(gates)
    assert "PASS" in table and "FAIL" in table and "passed 7/9" in table
