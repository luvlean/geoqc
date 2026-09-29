"""``geoqc eval`` commands (CONTRACTS §6): detection, verification-sample, discrepancies,
error-analysis, compare-secondary, gates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from geoqc.config import load_config
from geoqc.eval.common import (
    VERDICTS,
    EvalInputError,
    EvalPaths,
    load_eval_config,
    now_iso,
    read_git_commit,
    rel,
    write_json,
)

app = typer.Typer(
    no_args_is_help=True, help="Metrics, verification sampling, error analysis and quality gates."
)

_REPORTS_OPT = typer.Option(None, "--reports-dir", help="Override reports/ directory.")
_LABELS_DIR_OPT = typer.Option(None, "--labels-dir", help="Override data/labels/ directory.")
_TILES_INDEX_OPT = typer.Option(None, "--tiles-index", help="Override data/tiles/tiles.gpkg.")
_TILES_DIR_OPT = typer.Option(None, "--tiles-dir", help="Override data/tiles/ directory.")


def _fail(message: str, code: int = 2) -> None:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code)


def _paths(**overrides: Path | None) -> tuple[EvalPaths, dict[str, Any]]:
    cfg = load_config()
    return EvalPaths.from_config(cfg, **overrides), cfg


def _append_runs_log(paths: EvalPaths, outcome: str, **fields: Any) -> None:
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    parts = [now_iso(), f"outcome={outcome}", f"commit={read_git_commit(paths.repo_root) or 'n/a'}"]
    parts += [
        f"{k}={v!r}" if isinstance(v, str) and " " in v else f"{k}={v}" for k, v in fields.items()
    ]
    with paths.test_runs_log.open("a", encoding="utf-8") as fh:
        fh.write(" ".join(parts) + "\n")


# --------------------------------------------------------------------------- detection
@app.command()
def detection(
    split: str = typer.Option(..., "--split", help="val | test"),
    labels: Path | None = typer.Option(
        None, help="Labels GeoJSON (default data/labels/<split>/buildings.geojson)."
    ),
    detections: Path | None = typer.Option(
        None, help="Detections GeoJSON (default data/interim/detections.geojson)."
    ),
    min_score: float | None = typer.Option(
        None, help="Keep detections with score >= value before matching."
    ),
    method: str | None = typer.Option(None, help="greedy | hungarian (default config/eval.yaml)."),
    iou_threshold: float | None = typer.Option(
        None, help="TP threshold (default config/eval.yaml)."
    ),
    allow_unverified: bool = typer.Option(
        False, help="Smoke run on unverified labels; output is marked NOT A METRIC."
    ),
    force: bool = typer.Option(
        False, help="Overwrite an existing reports/metrics_test.json (requires --reason)."
    ),
    reason: str | None = typer.Option(None, help="Why the test set is evaluated again (logged)."),
    out: Path | None = typer.Option(
        None, help="Output JSON (default reports/metrics_<split>[_smoke].json)."
    ),
    reports_dir: Path | None = _REPORTS_OPT,
    labels_dir: Path | None = _LABELS_DIR_OPT,
    tiles_index: Path | None = _TILES_INDEX_OPT,
    tiles_dir: Path | None = _TILES_DIR_OPT,
) -> None:
    """Detection P/R/F1 and mean IoU on a split -> reports/metrics_<split>.json.

    `--split test` is one-shot: a second run is refused unless --force --reason is given; every
    attempt is appended to reports/metrics_test.runs.log.
    """
    from geoqc.eval.detection_metrics import evaluate_split

    if split not in ("val", "test"):
        _fail("--split must be val or test")
    paths, cfg = _paths(
        reports_dir=reports_dir,
        labels_dir=labels_dir,
        tiles_index=tiles_index,
        tiles_dir=tiles_dir,
        detections=detections,
    )
    try:
        ev = evaluate_split(
            split,
            paths,
            cfg,
            labels_path=labels,
            min_score=min_score,
            method=method,
            iou_threshold=iou_threshold,
        )
    except EvalInputError as exc:
        if split == "test":
            _append_runs_log(paths, "failed", split=split, error=str(exc))
        _fail(str(exc))
        return
    smoke = not ev.labels_verified
    if smoke and not allow_unverified:
        _fail(
            f"labels {ev.labels_path} are not all verified=true; use --allow-unverified for a smoke run (not a metric)"
        )
    target = Path(out) if out else paths.metrics(split, smoke=smoke)
    outcome = "smoke" if smoke else "completed"
    if split == "test" and not smoke and target.exists():
        if not force:
            _append_runs_log(
                paths,
                "refused",
                split=split,
                out=rel(target, paths.repo_root),
                reason="metrics_test.json exists; --force not given",
            )
            _fail(
                f"{rel(target, paths.repo_root)} already exists: the test set is evaluated once. Re-run with --force --reason '<why>' (logged in {rel(paths.test_runs_log, paths.repo_root)}).",
                code=3,
            )
        if not reason:
            _append_runs_log(
                paths,
                "refused",
                split=split,
                out=rel(target, paths.repo_root),
                reason="--force without --reason",
            )
            _fail("--force requires --reason")
        outcome = "forced"
    report = ev.to_report(now_iso(), read_git_commit(paths.repo_root), smoke)
    write_json(target, report)
    if split == "test":
        _append_runs_log(
            paths,
            outcome,
            split=split,
            out=rel(target, paths.repo_root),
            model_version=ev.model_version,
            f1=f"{report['detection']['f1']:.4f}",
            reason=reason or "",
        )
    s = report["detection"]
    if smoke:
        typer.secho(
            "NOT A METRIC: labels are not verified (smoke run)", fg=typer.colors.YELLOW, err=True
        )
    typer.echo(
        f"{split}: tiles={report['n_tiles']} gt={report['n_gt']} det={report['n_det']} "
        f"tp={s['tp']} fp={s['fp']} fn={s['fn']} P={s['precision']:.4f} R={s['recall']:.4f} F1={s['f1']:.4f} meanIoU={s['mean_iou_matched']:.4f}"
    )
    for note in report["notes"]:
        typer.echo(f"note: {note}")
    typer.echo(f"written {rel(target, paths.repo_root)}")


# --------------------------------------------------------------------------- verification sample
def _load_discrepancy_rows(paths: EvalPaths, from_geojson: Path | None, model_version: str | None):
    """(rows, source) from the DB, else from reports/discrepancies.geojson."""
    from geoqc.db import connect, db_available
    from geoqc.eval.discrepancy_metrics import fetch_discrepancies_db, fetch_discrepancies_geojson

    if from_geojson is not None:
        return fetch_discrepancies_geojson(from_geojson), rel(from_geojson, paths.repo_root)
    if db_available():
        with connect() as conn:
            return fetch_discrepancies_db(conn, model_version), "db:discrepancies"
    export = paths.reports_dir / "discrepancies.geojson"
    if export.exists():
        return fetch_discrepancies_geojson(export), rel(export, paths.repo_root)
    raise EvalInputError(
        "no discrepancies source: PostGIS unreachable and reports/discrepancies.geojson missing (run `geoqc matching run` / `export`)"
    )


@app.command("verification-sample")
def verification_sample(
    sample_size: int | None = typer.Option(
        None, help="Minimum sample size (default config/eval.yaml)."
    ),
    per_class_min: int | None = typer.Option(
        None, help="Minimum per class (default config/eval.yaml)."
    ),
    seed: int | None = typer.Option(
        None, help="Seed of the random part (default config/eval.yaml)."
    ),
    model_version: str | None = typer.Option(
        None, help="Restrict to one model_version (DB source only)."
    ),
    from_geojson: Path | None = typer.Option(
        None, help="Sample from a discrepancies GeoJSON instead of the DB."
    ),
    out: Path | None = typer.Option(
        None, help="Output CSV (default reports/verification_sample.csv)."
    ),
    reports_dir: Path | None = _REPORTS_OPT,
) -> None:
    """Stratified sample of discrepancies for manual verification -> reports/verification_sample.csv."""
    from geoqc.eval.discrepancy_metrics import stratified_sample, write_sample_csv

    paths, cfg = _paths(reports_dir=reports_dir)
    ecfg = load_eval_config(cfg)
    try:
        rows, source = _load_discrepancy_rows(paths, from_geojson, model_version)
        sample, meta = stratified_sample(
            rows,
            sample_size=int(sample_size or ecfg["sample_size"]),
            per_class_min=int(per_class_min or ecfg["per_class_min"]),
            top_k_global=int(ecfg["gate_precision_at_n"]),
            seed=int(seed if seed is not None else ecfg["seed"]),
        )
    except EvalInputError as exc:
        _fail(str(exc))
        return
    target = Path(out) if out else paths.reports_dir / "verification_sample.csv"
    write_sample_csv(sample, target)
    meta.update(
        {
            "generated_at": now_iso(),
            "source": source,
            "model_version": model_version,
            "output": rel(target, paths.repo_root),
        }
    )
    write_json(target.with_suffix(".meta.json"), meta)
    typer.echo(
        f"sampled {meta['n_selected']} of {meta['n_available']} discrepancies ({source}); by class {meta['selected_by_class']}"
    )
    if meta["short_classes"]:
        typer.echo(f"note: classes below per_class_min: {meta['short_classes']}")
    typer.echo(
        f"written {rel(target, paths.repo_root)} — fill `verdict` ({' | '.join(VERDICTS)}) "
        "and save as reports/verification.csv"
    )


# --------------------------------------------------------------------------- discrepancies
@app.command()
def discrepancies(
    verification: Path = typer.Option(
        ..., "--verification", help="Filled reports/verification.csv."
    ),
    model_version: str | None = typer.Option(
        None, help="Restrict counts_by_class to one model_version."
    ),
    from_geojson: Path | None = typer.Option(
        None, help="Take counts_by_class from a GeoJSON instead of the DB."
    ),
    out: Path | None = typer.Option(
        None, help="Output JSON (default reports/metrics_discrepancies.json)."
    ),
    reports_dir: Path | None = _REPORTS_OPT,
) -> None:
    """Precision per class, overall and Precision@N from the student's verdicts -> reports/metrics_discrepancies.json."""
    from geoqc.eval.discrepancy_metrics import (
        counts_by_class,
        read_verification_csv,
        verification_metrics,
    )

    paths, cfg = _paths(reports_dir=reports_dir)
    ecfg = load_eval_config(cfg)
    try:
        df = read_verification_csv(verification)
    except EvalInputError as exc:
        _fail(str(exc))
        return
    result = verification_metrics(
        df, [int(n) for n in ecfg["precision_at_n"]], tuple(ecfg["verdicts"])
    )
    counts: dict[str, int] | None = None
    counts_source: str | None = None
    notes: list[str] = []
    try:
        rows, counts_source = _load_discrepancy_rows(paths, from_geojson, model_version)
        counts = counts_by_class(rows)
    except EvalInputError as exc:
        notes.append(f"counts_by_class unavailable: {exc}")
    if result["invalid_verdicts"]:
        notes.append(
            f"invalid verdicts ignored: {result['invalid_verdicts']} (allowed: {ecfg['verdicts']})"
        )
    payload = {
        "schema_version": "1.0",
        "generated_at": now_iso(),
        "git_commit": read_git_commit(paths.repo_root),
        "verification_path": rel(verification, paths.repo_root),
        "counts_by_class": counts,
        "counts_source": counts_source,
        "gate_precision_at_n": int(ecfg["gate_precision_at_n"]),
        **result,
        "notes": notes,
    }
    target = Path(out) if out else paths.reports_dir / "metrics_discrepancies.json"
    write_json(target, payload)
    typer.echo(
        f"checked {result['n_checked']}/{result['n_rows']} rows; precision_overall={result['precision_overall']}"
    )
    for cls, block in result["verified_by_class"].items():
        typer.echo(
            f"  {cls:<18} n={block['n']:<3} true_error={block['true_error']:<3} unsure={block['unsure']:<3} precision={block['precision']}"
        )
    for n, block in result["precision_at_n"].items():
        typer.echo(f"  precision@{n}: {block['value']} (checked {block['n_available']}/{n})")
    for note in notes:
        typer.echo(f"note: {note}")
    typer.echo(f"written {rel(target, paths.repo_root)}")


# --------------------------------------------------------------------------- error analysis
@app.command("error-analysis")
def error_analysis(
    split: str = typer.Option("val", "--split", help="val (default) | test"),
    labels: Path | None = typer.Option(None, help="Labels GeoJSON override."),
    detections: Path | None = typer.Option(None, help="Detections GeoJSON override."),
    allow_unverified: bool = typer.Option(
        False, help="Allow unverified labels (report marked not a metric)."
    ),
    no_figures: bool = typer.Option(False, help="Skip PNG rendering."),
    out: Path | None = typer.Option(
        None, help="Markdown output (default reports/error_analysis.md)."
    ),
    reports_dir: Path | None = _REPORTS_OPT,
    labels_dir: Path | None = _LABELS_DIR_OPT,
    tiles_index: Path | None = _TILES_INDEX_OPT,
    tiles_dir: Path | None = _TILES_DIR_OPT,
) -> None:
    """FP/FN by tile and stratum, area/IoU distributions, figures -> reports/error_analysis.md (+ .json)."""
    from geoqc.eval.detection_metrics import evaluate_split
    from geoqc.eval.error_analysis import run_error_analysis

    if split not in ("val", "test"):
        _fail("--split must be val or test")
    paths, cfg = _paths(
        reports_dir=reports_dir,
        labels_dir=labels_dir,
        tiles_index=tiles_index,
        tiles_dir=tiles_dir,
        detections=detections,
    )
    try:
        ev = evaluate_split(split, paths, cfg, labels_path=labels)
    except EvalInputError as exc:
        _fail(str(exc))
        return
    if not ev.labels_verified and not allow_unverified:
        _fail(f"labels {ev.labels_path} are not all verified=true; use --allow-unverified")
    analysis, md_path = run_error_analysis(
        ev, paths, load_eval_config(cfg), out_md=out, render=not no_figures
    )
    s = analysis["summary"]
    typer.echo(
        f"{split}: tp={s['tp']} fp={s['fp']} fn={s['fn']} F1={s['f1']:.4f}; worst tiles: {[t['tile_id'] for t in analysis['worst_tiles']]}"
    )
    typer.echo(
        f"written {rel(md_path, paths.repo_root)} and {rel(md_path.with_suffix('.json'), paths.repo_root)}"
    )


# --------------------------------------------------------------------------- compare secondary
@app.command("compare-secondary")
def compare_secondary(
    split: str = typer.Option("test", "--split", help="test (default) | val"),
    secondary: Path | None = typer.Option(
        None,
        help="Secondary footprints GeoJSON (default data/interim/secondary_buildings.geojson, else buildings_ref).",
    ),
    labels: Path | None = typer.Option(None, help="Labels GeoJSON override."),
    detections: Path | None = typer.Option(None, help="Detections GeoJSON override."),
    allow_unverified: bool = typer.Option(
        False, help="Compare against unverified labels (marked not a metric)."
    ),
    out: Path | None = typer.Option(None, help="Output JSON (default reports/comparison.json)."),
    reports_dir: Path | None = _REPORTS_OPT,
    labels_dir: Path | None = _LABELS_DIR_OPT,
    tiles_index: Path | None = _TILES_INDEX_OPT,
    tiles_dir: Path | None = _TILES_DIR_OPT,
) -> None:
    """Detections and secondary footprints vs manual labels (P/R/F1) -> reports/comparison.json."""
    from geoqc.db import connect, db_available
    from geoqc.eval.compare_secondary import compare

    if split not in ("val", "test"):
        _fail("--split must be val or test")
    paths, cfg = _paths(
        reports_dir=reports_dir,
        labels_dir=labels_dir,
        tiles_index=tiles_index,
        tiles_dir=tiles_dir,
        detections=detections,
    )
    try:
        if secondary is None and not paths.secondary.exists() and db_available():
            with connect() as conn:
                result = compare(
                    split,
                    paths,
                    cfg,
                    labels_path=labels,
                    allow_unverified=allow_unverified,
                    conn=conn,
                )
        else:
            result = compare(
                split,
                paths,
                cfg,
                secondary_path=secondary,
                labels_path=labels,
                allow_unverified=allow_unverified,
            )
    except EvalInputError as exc:
        _fail(str(exc))
        return
    target = Path(out) if out else paths.reports_dir / "comparison.json"
    write_json(target, result)
    for key in ("detection_vs_gt", "secondary_vs_gt", "detection_vs_secondary"):
        block = result.get(key)
        typer.echo(
            f"{key}: "
            + (
                f"P={block['precision']:.4f} R={block['recall']:.4f} F1={block['f1']:.4f} (tp={block['tp']} fp={block['fp']} fn={block['fn']})"
                if block
                else "null"
            )
        )
    for note in result["notes"]:
        typer.echo(f"note: {note}")
    typer.echo(f"written {rel(target, paths.repo_root)}")


# --------------------------------------------------------------------------- gates
@app.command()
def gates(
    make_all_passed: bool = typer.Option(
        False,
        "--make-all-passed/--no-make-all-passed",
        help="G8: `make all` succeeded in a clean environment.",
    ),
    tests_passed: bool = typer.Option(
        False, "--tests-passed/--no-tests-passed", help="G9: pytest passed."
    ),
    n_tests: int = typer.Option(0, "--n-tests", help="Number of tests that ran."),
    out: Path | None = typer.Option(None, help="Output JSON (default reports/metrics.json)."),
    reports_dir: Path | None = _REPORTS_OPT,
) -> None:
    """Assemble reports/metrics.json (CONTRACTS §3.9) from all intermediate reports and evaluate G1–G9."""
    from geoqc.eval.gates import build_metrics, format_gates_table

    paths, cfg = _paths(reports_dir=reports_dir)
    metrics = build_metrics(
        paths, cfg, make_all_passed=make_all_passed, tests_passed=tests_passed, n_tests=n_tests
    )
    target = Path(out) if out else paths.reports_dir / "metrics.json"
    write_json(target, metrics)
    typer.echo(format_gates_table(metrics["gates"]))
    typer.echo(f"written {rel(target, paths.repo_root)}")
