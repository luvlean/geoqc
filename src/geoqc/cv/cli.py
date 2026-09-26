"""``geoqc cv`` commands: fetch-weights, predict, export-dataset, threshold-sweep, bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from geoqc.config import load_config

app = typer.Typer(
    no_args_is_help=True, help="Building detection: weights, predict, dataset, sweep, bundle"
)


def _echo(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


@app.command("fetch-weights")
def fetch_weights_cmd(
    force: Annotated[
        bool, typer.Option("--force", help="Re-download even if the file exists")
    ] = False,
    fallback: Annotated[
        bool, typer.Option("--fallback/--no-fallback", help="Allow the COCO yolov8m-seg fallback")
    ] = True,
) -> None:
    """Download the building-segmentation checkpoint, hash it, write model_version to config."""
    from geoqc.cv.weights import fetch_weights

    cfg = load_config()
    info = fetch_weights(cfg["cv"], force=force, allow_fallback=fallback)
    _echo(info.to_dict())
    if info.is_fallback:
        typer.secho(
            "WARNING: fallback COCO weights - no building class, fine-tune before use",
            fg=typer.colors.YELLOW,
            err=True,
        )


@app.command("predict")
def predict_cmd(
    split: Annotated[
        str, typer.Option("--split", help="all | val | test (tiles to run on)")
    ] = "all",
    to_db: Annotated[bool, typer.Option("--to-db", help="Also load into buildings_det")] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Process only the first N tiles")
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Output GeoJSON (default data/interim/detections[_split].geojson)"
        ),
    ] = None,
    device: Annotated[str | None, typer.Option("--device", help="auto | cuda | mps | cpu")] = None,
    conf: Annotated[float | None, typer.Option("--conf", help="Override conf_threshold")] = None,
    backend: Annotated[
        str | None, typer.Option("--backend", help="ultralytics | smp_unet | mock")
    ] = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace/--append", help="Delete rows of the same model_version before loading"
        ),
    ] = True,
    progress: Annotated[bool, typer.Option("--progress/--no-progress")] = True,
) -> None:
    """Run detection on tiles -> detections.geojson (EPSG:4326, MultiPolygon) [-> buildings_det]."""
    from geoqc.cv.pipeline import (
        default_detections_path,
        load_to_db,
        run_predict,
        summarize,
        write_detections,
    )

    cfg = load_config()
    result = run_predict(
        cfg["cv"],
        cfg,
        split=split,
        limit=limit,
        device=device,
        conf=conf,
        backend=backend,
        progress=progress,
    )
    out_path = write_detections(
        result.detections,
        out or default_detections_path(split, cfg["paths"].get("interim", "data/interim")),
    )
    summary = summarize(result)
    summary["detections_path"] = str(out_path)
    if to_db:
        summary["db_rows"] = load_to_db(result.detections, result.model_version, replace=replace)
    _echo(summary)


@app.command("export-dataset")
def export_dataset_cmd(
    labels: Annotated[
        Path | None,
        typer.Option("--labels", help="Labels GeoJSON (default data/labels/val/buildings.geojson)"),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Output directory (default data/interim/dataset)")
    ] = None,
    seed: Annotated[int | None, typer.Option("--seed")] = None,
    holdout_fraction: Annotated[float | None, typer.Option("--holdout-fraction")] = None,
    allow_unverified: Annotated[
        bool, typer.Option("--allow-unverified", help="Accept draft labels (smoke tests only)")
    ] = False,
) -> None:
    """Export val labels + tiles to YOLO-seg txt polygons and binary masks (train/holdout)."""
    from geoqc.cv.dataset import export_dataset

    cfg = load_config()
    summary = export_dataset(
        cfg["cv"],
        cfg,
        labels_path=labels,
        out_dir=out,
        seed=seed,
        holdout_fraction=holdout_fraction,
        require_verified=not allow_unverified,
    )
    _echo(summary.to_dict())


@app.command("threshold-sweep")
def threshold_sweep_cmd(
    split: Annotated[str, typer.Option("--split", help="Only 'val' is allowed")] = "val",
    holdout: Annotated[
        bool, typer.Option("--holdout", help="Restrict to the holdout subset of export-dataset")
    ] = False,
    detections: Annotated[
        Path | None,
        typer.Option(
            "--detections", help="Reuse an existing detections GeoJSON instead of running inference"
        ),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    device: Annotated[str | None, typer.Option("--device")] = None,
    backend: Annotated[str | None, typer.Option("--backend")] = None,
    update_config: Annotated[
        bool,
        typer.Option(
            "--update-config/--no-update-config",
            help="Write the best conf_threshold to config/cv.yaml",
        ),
    ] = True,
    allow_unverified: Annotated[
        bool, typer.Option("--allow-unverified", help="Accept draft labels (smoke tests only)")
    ] = False,
    progress: Annotated[bool, typer.Option("--progress/--no-progress")] = True,
) -> None:
    """F1@IoU>=0.5 for a grid of confidence thresholds on val -> reports/threshold_sweep_val.json."""
    from geoqc.cv.sweep import run_sweep

    cfg = load_config()
    report = run_sweep(
        cfg["cv"],
        cfg,
        split=split,
        holdout_only=holdout,
        detections_path=detections,
        limit=limit,
        device=device,
        backend=backend,
        require_verified=not allow_unverified,
        update_config=update_config,
        progress=progress,
    )
    table = [
        {k: r[k] for k in ("conf", "n_det", "tp", "fp", "fn", "precision", "recall", "f1")}
        for r in report["thresholds"]
    ]
    _echo(
        {
            "best": report["best"],
            "n_gt": report["n_gt"],
            "n_tiles": report["n_tiles"],
            "metrics_impl": report["metrics_impl"],
            "report_path": report["report_path"],
            "thresholds": table,
        }
    )


@app.command("bundle")
def bundle_cmd(
    tiles: Annotated[str | None, typer.Option("--tiles", help="all | val")] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Output zip (default data/interim/bundle.zip)")
    ] = None,
    include_dataset: Annotated[
        bool,
        typer.Option("--dataset/--no-dataset", help="Include data/interim/dataset when present"),
    ] = True,
) -> None:
    """Zip tiles, val labels, split csv and config for the Colab notebook (never the test labels)."""
    from geoqc.cv.bundle import build_bundle

    cfg = load_config()
    summary = build_bundle(cfg["cv"], cfg, out=out, tiles=tiles, include_dataset=include_dataset)
    _echo(summary.to_dict())


if __name__ == "__main__":
    app()
