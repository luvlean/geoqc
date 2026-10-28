"""``geoqc cv`` commands: fetch-weights, predict, export-dataset, prepare-microsoft, threshold-sweep, bundle."""

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
    backend = cfg["cv"].get("backend", "ultralytics")
    if backend != "ultralytics":
        typer.echo(f"backend={backend}: weights come from the notebook export, nothing to fetch")
        return
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
        str, typer.Option("--split", help="all | train | val | test (tiles to run on)")
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
            "--replace/--append",
            help="Delete all previous detections and discrepancies before loading",
        ),
    ] = True,
    progress: Annotated[bool, typer.Option("--progress/--no-progress")] = True,
    weights: Annotated[
        Path | None,
        typer.Option(
            "--weights",
            help="Try other weights without touching config/cv.yaml (no --to-db; use with --out)",
        ),
    ] = None,
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
    model_version = None
    if weights is not None:
        from geoqc.cv.weights import model_version_for, sha256_of

        if to_db:
            # An experiment must not replace the review queue: promote the weights in config first.
            raise typer.BadParameter("--weights cannot be combined with --to-db")
        if not weights.exists():
            raise typer.BadParameter(f"weights not found: {weights}")
        cv = dict(cfg["cv"])
        cv["weights"] = {**cv.get("weights", {}), "path": str(weights)}
        cv["smp_unet"] = {**cv.get("smp_unet", {}), "checkpoint": str(weights)}
        cfg["cv"] = cv
        model_version = model_version_for(weights.stem, sha256_of(weights))
    result = run_predict(
        cfg["cv"],
        cfg,
        split=split,
        limit=limit,
        device=device,
        conf=conf,
        backend=backend,
        progress=progress,
        model_version=model_version,
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


@app.command("prepare-microsoft")
def prepare_microsoft_cmd(
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output directory (default cv.microsoft.out_dir)"),
    ] = None,
) -> None:
    """Microsoft footprints on tiles away from val/test -> YOLO-seg training set (pseudo-labels)."""
    from geoqc.cv.microsoft import export_microsoft_dataset

    cfg = load_config()
    _echo(export_microsoft_dataset(cfg["cv"], cfg, out_dir=out).to_dict())


@app.command("fetch-external")
def fetch_external_cmd(
    cities: Annotated[
        list[str] | None, typer.Option("--city", help="SpaceNet AOI, repeatable (AOI_3_Paris)")
    ] = None,
    per_city: Annotated[
        int | None, typer.Option("--per-city", help="Tiles per city, 0 = all")
    ] = None,
) -> None:
    """Download a labelled SpaceNet 2 sample (CC BY-SA 4.0) -> data/raw/external/spacenet2."""
    from geoqc.cv.external import fetch_spacenet

    cfg = load_config()
    _echo(fetch_spacenet(cfg["cv"], cities=cities, per_city=per_city))


@app.command("prepare-external")
def prepare_external_cmd(
    cities: Annotated[list[str] | None, typer.Option("--city", help="Repeatable")] = None,
    out: Annotated[Path | None, typer.Option("--out")] = None,
) -> None:
    """SpaceNet tiles -> 0.3 m UTM chips, YOLO-seg labels and masks (train/holdout)."""
    from geoqc.cv.external import prepare_spacenet

    cfg = load_config()
    _echo(prepare_spacenet(cfg["cv"], cities=cities, out_dir=out))


@app.command("report-external")
def report_external_cmd(
    per_city: Annotated[int, typer.Option("--per-city", help="Sample chips per city")] = 4,
) -> None:
    """Stats + figures of the SpaceNet sample vs our AOI -> reports/, docs/datasets/figures/."""
    from geoqc.cv.external_report import build_report

    cfg = load_config()
    report = build_report(cfg["cv"], cfg, per_city=per_city)
    _echo({"figures": report["figures"], "spacenet": report["spacenet"]})


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
