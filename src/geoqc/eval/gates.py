"""Assemble ``reports/metrics.json`` (CONTRACTS §3.9) and evaluate gates G1–G9 (quality_gates.yaml)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from geoqc.eval.common import (
    DISCREPANCY_CLASSES,
    EvalPaths,
    load_eval_config,
    now_iso,
    pick,
    read_git_commit,
    read_git_dirty,
    read_json,
    rel,
)
from geoqc.eval.georef import read_georef

GATE_OPS: tuple[str, ...] = ("<=", ">=", "==", "<", ">", "!=")

# §3.9 template with null values. Every key here must exist in the produced file.
METRICS_TEMPLATE: dict[str, Any] = {
    "schema_version": "1.0",
    "generated_at": None,
    "git_commit": None,
    "git_dirty": None,
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
        "counts_by_class": {cls: None for cls in DISCREPANCY_CLASSES},
        "verified_by_class": {
            cls: {"n": None, "true_error": None, "precision": None} for cls in DISCREPANCY_CLASSES
        },
        "precision_overall": None,
        "precision_at_n": {"n": None, "value": None},
    },
    "comparison": {
        "secondary_source": None,
        "detection_vs_secondary": {"precision": None, "recall": None, "f1": None},
    },
    "reproducibility": {"make_all_passed": False},
    "tests": {"passed": False, "n_tests": 0},
    "gates": {},
}

# Where a metric comes from and how to produce it (used in gate notes when the metric is missing).
_SOURCE_HINTS: tuple[tuple[str, str, str], ...] = (
    ("georeference.", "reports/georef.json", "geoqc data check-georef"),
    ("annotation.", "reports/annotation.json", "geoqc annotation validate --split val"),
    ("detection.val.", "reports/metrics_val.json", "geoqc eval detection --split val"),
    ("detection.test.", "reports/metrics_test.json", "geoqc eval detection --split test"),
    (
        "discrepancies.",
        "reports/metrics_discrepancies.json",
        "geoqc eval discrepancies --verification reports/verification.csv",
    ),
    ("comparison.", "reports/comparison.json", "geoqc eval compare-secondary"),
    ("reproducibility.", "flag", "geoqc eval gates --make-all-passed"),
    ("tests.", "flag", "geoqc eval gates --tests-passed --n-tests N"),
)


def get_by_path(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def evaluate_gate(op: str, target: Any, actual: Any) -> tuple[bool, str]:
    """Apply ``actual <op> target``; missing metric or unknown op -> (False, note)."""
    if actual is None:
        return False, "metric missing"
    if op not in GATE_OPS:
        return False, f"unknown op {op!r}"
    try:
        if op == "==":
            passed = bool(actual == target)
        elif op == "!=":
            passed = bool(actual != target)
        else:
            a, t = float(actual), float(target)
            passed = {"<=": a <= t, ">=": a >= t, "<": a < t, ">": a > t}[op]
    except (TypeError, ValueError) as exc:
        return False, f"cannot compare {actual!r} {op} {target!r}: {exc}"
    return passed, ""


def _source_hint(metric: str) -> str:
    for prefix, source, command in _SOURCE_HINTS:
        if metric.startswith(prefix):
            return f"{source} (`{command}`)"
    return "unknown source"


def evaluate_gates(
    metrics: dict[str, Any], gate_cfg: dict[str, Any], missing_sources: dict[str, str]
) -> dict[str, Any]:
    """Return the ``gates`` block: target/op/actual/passed/note per gate id."""
    out: dict[str, Any] = {}
    for gate_id in sorted(gate_cfg, key=lambda g: (len(g), g)):
        spec = gate_cfg[gate_id] or {}
        metric = str(spec.get("metric", ""))
        op = str(spec.get("op", ">="))
        target = spec.get("target")
        actual = get_by_path(metrics, metric) if metric else None
        passed, note = evaluate_gate(op, target, actual)
        if actual is None:
            reason = None
            for prefix, text in missing_sources.items():
                if metric.startswith(prefix):
                    reason = text
                    break
            note = f"metric `{metric}` is null: {reason or 'key absent in ' + _source_hint(metric)}"
        out[gate_id] = {
            "metric": metric,
            "description": spec.get("description"),
            "target": target,
            "op": op,
            "actual": actual,
            "passed": bool(passed),
            "note": note,
        }
    return out


def _detection_block(
    report: dict[str, Any] | None, split: str, paths: EvalPaths, missing: dict[str, str]
) -> dict[str, Any] | None:
    path = paths.metrics(split)
    if report is None:
        missing[f"detection.{split}."] = (
            f"{rel(path, paths.repo_root)} not found (run `geoqc eval detection --split {split}`)"
        )
        return None
    if report.get("is_metric") is False or report.get("labels_verified") is False:
        missing[f"detection.{split}."] = (
            f"{rel(path, paths.repo_root)} is a smoke run on unverified labels, not a metric"
        )
        return None
    block = report.get("detection")
    if not isinstance(block, dict):
        missing[f"detection.{split}."] = f"{rel(path, paths.repo_root)} has no `detection` block"
        return None
    return block


def build_metrics(
    paths: EvalPaths,
    cfg: dict[str, Any],
    *,
    make_all_passed: bool = False,
    tests_passed: bool = False,
    n_tests: int = 0,
    generated_at: str | None = None,
    git_commit: str | None = None,
) -> dict[str, Any]:
    """Merge every intermediate report into the fixed §3.9 schema and evaluate the gates."""
    ecfg = load_eval_config(cfg)
    metrics = copy.deepcopy(METRICS_TEMPLATE)
    metrics["generated_at"] = generated_at or now_iso()
    metrics["git_commit"] = (
        git_commit if git_commit is not None else read_git_commit(paths.repo_root)
    )
    # code changed after the commit above: the numbers are not reproducible from it alone
    metrics["git_dirty"] = None if git_commit is not None else read_git_dirty(paths.repo_root)
    missing: dict[str, str] = {}
    inputs: dict[str, str | None] = {}

    def load(name: str, path: Path) -> dict[str, Any] | None:
        data = read_json(path)
        inputs[name] = rel(path, paths.repo_root) if data is not None else None
        return data

    val = load("metrics_val", paths.metrics("val"))
    test = load("metrics_test", paths.metrics("test"))
    georef_raw = load("georef", paths.reports_dir / "georef.json")
    annotation = load("annotation", paths.reports_dir / "annotation.json")
    disc = load("metrics_discrepancies", paths.reports_dir / "metrics_discrepancies.json")
    comparison = load("comparison", paths.reports_dir / "comparison.json")
    manifest = load("manifest", paths.manifest)
    aoi_geojson = load("aoi", paths.aoi)

    # dataset / model -------------------------------------------------------
    aoi_name = pick(manifest, "aoi.name")
    if aoi_name is None and aoi_geojson:
        feats = aoi_geojson.get("features") or []
        aoi_name = pick(feats[0] if feats else aoi_geojson, "properties.name", "name")
    metrics["dataset"]["aoi"] = aoi_name
    metrics["dataset"]["n_tiles_val"] = pick(val, "n_tiles")
    metrics["dataset"]["n_tiles_test"] = pick(test, "n_tiles")
    metrics["dataset"]["n_gt_val"] = pick(val, "n_gt")
    metrics["dataset"]["n_gt_test"] = pick(test, "n_gt")
    cv_cfg = cfg.get("cv") or {}
    metrics["model"]["model_version"] = (
        pick(test, "model_version") or pick(val, "model_version") or cv_cfg.get("model_version")
    )
    conf = pick(test, "conf_threshold")
    if conf is None:
        conf = pick(val, "conf_threshold")
    if conf is None:
        conf = cv_cfg.get("conf_threshold")
    metrics["model"]["conf_threshold"] = conf
    metrics["model"]["iou_threshold"] = float(ecfg["iou_threshold"])

    # georeference / annotation ------------------------------------------------
    georef = read_georef(paths.reports_dir / "georef.json", paths.manifest)
    metrics["georeference"]["median_offset_m"] = georef["median_offset_m"]
    metrics["georeference"]["n_control_points"] = georef["n_control_points"]
    if georef["median_offset_m"] is None:
        missing["georeference."] = georef["note"] or "reports/georef.json unusable"
    if georef_raw is None and georef["source"]:
        inputs["georef"] = georef["source"]

    if annotation is None:
        missing["annotation."] = (
            "reports/annotation.json not found (run `geoqc annotation validate --split val`)"
        )
    else:
        metrics["annotation"]["agreement_iou"] = pick(
            annotation,
            "annotation.agreement_iou",  # geoqc annotation validate
            "agreement_iou",
            "g2.agreement_iou",
            "recheck.agreement_iou",
            "recheck.mean_iou",
            "agreement.iou",
            "mean_iou",
        )
        metrics["annotation"]["agreement_f1"] = pick(
            annotation,
            "annotation.agreement_f1",  # geoqc annotation validate
            "agreement_f1",
            "g2.agreement_f1",
            "recheck.agreement_f1",
            "recheck.f1",
            "agreement.f1",
            "f1",
        )
        metrics["annotation"]["n_checked"] = pick(
            annotation,
            "annotation.n_checked",  # geoqc annotation validate
            "n_checked",
            "g2.n_checked",
            "recheck.n_checked",
            "recheck.n_pairs",
            "recheck.n_tiles",
            "n_recheck",
        )
        if metrics["annotation"]["agreement_iou"] is None:
            missing["annotation."] = (
                "reports/annotation.json has no recheck agreement (G2 not computed yet)"
            )

    # detection ------------------------------------------------------------
    for split, report in (("val", val), ("test", test)):
        block = _detection_block(report, split, paths, missing)
        if block is not None:
            for key in metrics["detection"][split]:
                metrics["detection"][split][key] = block.get(key)

    # discrepancies ----------------------------------------------------------
    gate_n = int(ecfg["gate_precision_at_n"])
    metrics["discrepancies"]["precision_at_n"]["n"] = gate_n
    if disc is None:
        missing["discrepancies."] = (
            "reports/metrics_discrepancies.json not found (run `geoqc eval discrepancies --verification reports/verification.csv`)"
        )
    else:
        counts = disc.get("counts_by_class") or {}
        for cls in DISCREPANCY_CLASSES:
            metrics["discrepancies"]["counts_by_class"][cls] = counts.get(cls)
            block = (disc.get("verified_by_class") or {}).get(cls) or {}
            metrics["discrepancies"]["verified_by_class"][cls] = {
                "n": block.get("n"),
                "true_error": block.get("true_error"),
                "precision": block.get("precision"),
                "unsure": block.get("unsure"),
                "model_error": block.get("model_error"),
                "image_issue": block.get("image_issue"),
            }
        metrics["discrepancies"]["precision_overall"] = disc.get("precision_overall")
        at_n = (disc.get("precision_at_n") or {}).get(str(gate_n)) or {}
        metrics["discrepancies"]["precision_at_n"]["value"] = at_n.get("value")
        metrics["discrepancies"]["precision_at_n"]["n_available"] = at_n.get("n_available")
        metrics["discrepancies"]["precision_at_n"]["value_incl_unsure"] = at_n.get(
            "value_incl_unsure"
        )
        metrics["discrepancies"]["n_checked"] = disc.get("n_checked")
        if disc.get("precision_overall") is None:
            missing["discrepancies.precision_overall"] = (
                "no checked verdicts in reports/verification.csv"
            )
        if at_n.get("value") is None:
            missing["discrepancies.precision_at_n"] = (
                f"fewer than 1 checked row for Precision@{gate_n}"
            )
        elif not at_n.get("complete", True):
            metrics["discrepancies"]["precision_at_n"]["note"] = (
                f"only {at_n.get('n_available')} checked rows for Precision@{gate_n}"
            )

    # comparison -----------------------------------------------------------------
    if comparison is None:
        missing["comparison."] = (
            "reports/comparison.json not found (run `geoqc eval compare-secondary`)"
        )
    else:
        metrics["comparison"]["secondary_source"] = comparison.get("secondary_source")
        dvs = comparison.get("detection_vs_secondary") or {}
        for key in ("precision", "recall", "f1"):
            metrics["comparison"]["detection_vs_secondary"][key] = dvs.get(key)
        metrics["comparison"]["detection_vs_gt"] = comparison.get("detection_vs_gt")
        metrics["comparison"]["secondary_vs_gt"] = comparison.get("secondary_vs_gt")

    # flags ----------------------------------------------------------------
    metrics["reproducibility"]["make_all_passed"] = bool(make_all_passed)
    metrics["tests"]["passed"] = bool(tests_passed)
    metrics["tests"]["n_tests"] = int(n_tests)

    metrics["gates"] = evaluate_gates(metrics, cfg.get("gates") or {}, missing)
    current = cv_cfg.get("model_version")
    for split, report in (("val", val), ("test", test)):
        used = pick(report, "model_version")
        if not current or not used or used == current:
            continue
        stale = f"{split} metrics are for {used}, config/cv.yaml has {current}"
        for g in metrics["gates"].values():
            if str(g.get("metric", "")).startswith(f"detection.{split}."):
                g["note"] = f"{g['note']}; {stale}" if g.get("note") else stale
    if disc and disc.get("n_not_in_queue"):
        stale = f"{disc['n_not_in_queue']} verified ids are not in the current queue"
        for g in metrics["gates"].values():
            if str(g.get("metric", "")).startswith("discrepancies."):
                g["note"] = f"{g['note']}; {stale}" if g.get("note") else stale
    metrics["inputs"] = inputs
    return metrics


def format_gates_table(gates: dict[str, Any]) -> str:
    """Plain-text table for the console."""
    header = f"{'gate':<4} {'metric':<38} {'op':<3} {'target':>7} {'actual':>9} {'result':<6} note"
    lines = [header, "-" * len(header)]
    for gate_id, g in gates.items():
        actual = g.get("actual")
        if isinstance(actual, bool):
            actual_s = str(actual)
        elif isinstance(actual, int | float):
            actual_s = f"{actual:.4g}"
        else:
            actual_s = "null"
        result = "PASS" if g.get("passed") else "FAIL"
        lines.append(
            f"{gate_id:<4} {g.get('metric', ''):<38} {g.get('op', ''):<3} {str(g.get('target')):>7} {actual_s:>9} {result:<6} {g.get('note') or ''}"
        )
    n_pass = sum(1 for g in gates.values() if g.get("passed"))
    lines.append(f"passed {n_pass}/{len(gates)}")
    return "\n".join(lines)
