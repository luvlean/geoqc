"""Typer CLI for the annotation module: ``geoqc annotation <command>`` (CONTRACTS §6)."""

from __future__ import annotations

from pathlib import Path

import typer

from geoqc.annotation.common import SPLITS, Paths, annotation_config, resolve_path

app = typer.Typer(
    no_args_is_help=True,
    help="Annotation: stratified tile selection, pre-annotation, QGIS workspace, finalize and validate (G2).",
)


def _paths(labels_dir: Path | None, tiles_index: Path | None) -> tuple[dict, Paths]:
    cfg = annotation_config()
    return cfg, Paths.from_config(cfg, labels_dir, tiles_index)


def _split(split: str) -> str:
    if split not in SPLITS:
        raise typer.BadParameter(f"split must be one of {SPLITS}")
    return split


LABELS_DIR_OPT = typer.Option(
    None, "--labels-dir", help="Override data/labels (tests, experiments)."
)
TILES_INDEX_OPT = typer.Option(None, "--tiles-index", help="Override data/tiles/tiles.gpkg.")


@app.command("select-tiles")
def select_tiles_cmd(
    osm: Path | None = typer.Option(
        None, "--osm", help="OSM buildings GeoJSON used only for stratification."
    ),
    n_tiles: int | None = typer.Option(
        None, "--n-tiles", help="Total tiles (default: config n_tiles)."
    ),
    seed: int | None = typer.Option(None, "--seed"),
    test_fraction: float | None = typer.Option(None, "--test-fraction"),
    labels_dir: Path | None = LABELS_DIR_OPT,
    tiles_index: Path | None = TILES_INDEX_OPT,
) -> None:
    """Stratified selection of annotation tiles -> data/labels/tiles_split.csv (+ recheck_tiles.csv)."""
    from geoqc.annotation.select_tiles import select_tiles

    cfg, paths = _paths(labels_dir, tiles_index)
    osm_path = osm or resolve_path(
        cfg["annotation"]
        .get("select_tiles", {})
        .get("osm_path", "data/interim/osm_buildings.geojson")
    )
    if not osm_path.exists():
        typer.secho(
            f"OSM buildings not found: {osm_path} (run `geoqc data osm`)", fg="red", err=True
        )
        raise typer.Exit(2)
    rep = select_tiles(
        cfg, paths, osm_path, n_tiles=n_tiles, seed=seed, test_fraction=test_fraction
    )
    typer.echo(
        f"tiles: {rep['n_tiles_total']} total, {rep['n_candidates']} candidates "
        f"(excluded: {rep['excluded_few_buildings']} few buildings, {rep['excluded_aoi_coverage']} outside AOI)"
    )
    typer.echo(f"candidates by stratum: {rep['candidates_by_stratum']}")
    typer.echo(
        f"selected {rep['selected']}: {rep['selected_by_split']} | by stratum {rep['selected_by_stratum']}"
    )
    typer.echo(f"recheck tiles (G2): {rep['recheck_tiles']}")
    typer.echo(f"-> {rep['outputs']['tiles_split']}")


@app.command("pre-annotate")
def pre_annotate_cmd(
    split: str = typer.Option(..., "--split", help="val | test"),
    source: str | None = typer.Option(
        None, "--source", help="secondary | detections (default: first available)."
    ),
    source_path: Path | None = typer.Option(
        None, "--source-path", help="Explicit polygon file to draft from."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing draft.geojson."),
    labels_dir: Path | None = LABELS_DIR_OPT,
    tiles_index: Path | None = TILES_INDEX_OPT,
) -> None:
    """Draft outlines clipped to the split tiles -> data/labels/<split>/draft.geojson (verified=false)."""
    from geoqc.annotation.pre_annotate import pre_annotate

    cfg, paths = _paths(labels_dir, tiles_index)
    try:
        rep = pre_annotate(
            cfg, paths, _split(split), source=source, source_path=source_path, force=force
        )
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    typer.echo(
        f"[{split}] source={rep['source']} ({rep['n_source_features']} polygons) -> "
        f"{rep['n_draft_features']} draft buildings on {rep['n_tiles']} tiles"
    )
    if rep["tiles_without_draft"]:
        typer.secho(f"tiles without draft polygons: {rep['tiles_without_draft']}", fg="yellow")
    typer.echo(f"-> {rep['output']}")


@app.command("qgis-project")
def qgis_project_cmd(
    split: str = typer.Option(..., "--split", help="val | test"),
    force: bool = typer.Option(
        False, "--force", help="Recreate work.gpkg from draft (keeps a .bak)."
    ),
    no_qgis: bool = typer.Option(
        False, "--no-qgis", help="Only write styles/working files, do not run PyQGIS."
    ),
    annotator: str = typer.Option("", "--annotator", help="Default value for the annotator field."),
    labels_dir: Path | None = LABELS_DIR_OPT,
    tiles_index: Path | None = TILES_INDEX_OPT,
) -> None:
    """Working GeoPackage, tile frames, .qml styles and (macOS) the .qgz project for a split."""
    from geoqc.annotation.qgis_project import build_workspace, fallback_instructions

    cfg, paths = _paths(labels_dir, tiles_index)
    rep = build_workspace(
        cfg, paths, _split(split), force=force, run_qgis=not no_qgis, annotator=annotator
    )
    typer.echo(f"[{split}] tiles: {rep['n_tiles']}, recheck tiles: {rep['recheck_tiles']}")
    typer.echo(f"work.gpkg: {rep['work_gpkg']}")
    if not rep["draft_present"]:
        typer.secho(
            "draft.geojson not found: `buildings` layer is empty (label from scratch)", fg="yellow"
        )
    if rep["layers_skipped_missing"]:
        typer.secho(f"layers skipped (file missing): {rep['layers_skipped_missing']}", fg="yellow")
    typer.echo(
        "Esri World Imagery layer: "
        + ("added (ARCGIS_API_KEY set)" if rep["esri_layer"] else "skipped (no ARCGIS_API_KEY)")
    )
    if rep["project"]:
        typer.secho(f"project: {rep['project']}", fg="green")
        warnings = (rep.get("qgis_result") or {}).get("warnings") or []
        for w in warnings:
            typer.secho(f"  warning: {w}", fg="yellow")
    else:
        if rep.get("qgis_result"):
            typer.secho(
                f"PyQGIS failed: {rep['qgis_result'].get('error') or rep['qgis_result'].get('stderr_tail', '')[-500:]}",
                fg="red",
            )
        typer.echo(fallback_instructions(rep))


@app.command("finalize")
def finalize_cmd(
    split: str = typer.Option(..., "--split", help="val | test"),
    annotator: str | None = typer.Option(
        None, "--annotator", help="Fill empty annotator fields with this name."
    ),
    input_path: Path | None = typer.Option(
        None, "--input", help="work.gpkg or a GeoJSON to finalize (default: auto)."
    ),
    labels_dir: Path | None = LABELS_DIR_OPT,
    tiles_index: Path | None = TILES_INDEX_OPT,
) -> None:
    """work.gpkg -> buildings.geojson / recheck.geojson (clip to tiles, fix geometry, normalise properties)."""
    from geoqc.annotation.finalize import finalize_split

    cfg, paths = _paths(labels_dir, tiles_index)
    try:
        rep = finalize_split(cfg, paths, _split(split), annotator=annotator, input_path=input_path)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    for name, st in rep["layers"].items():
        if st.get("output"):
            typer.echo(
                f"[{split}] {name}: {st['n_input']} -> {st['n_output']} features "
                f"(verified {st['n_verified']}, clipped {st['clipped']}, tile reassigned {st['tile_reassigned']}, "
                f"dropped small {st['dropped_small']}, dropped empty {st['dropped_empty']}) -> {st['output']}"
            )
        else:
            typer.echo(f"[{split}] {name}: no features (recheck.geojson not written)")


@app.command("validate")
def validate_cmd(
    split: str = typer.Option(..., "--split", help="val | test"),
    labels: Path | None = typer.Option(
        None, "--labels", help="Labels file (default: data/labels/<split>/buildings.geojson)."
    ),
    recheck: Path | None = typer.Option(
        None, "--recheck", help="Recheck file (default: data/labels/<split>/recheck.geojson)."
    ),
    report: Path | None = typer.Option(
        None, "--report", help="Report path (default: reports/annotation.json)."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit 2 on warnings or a failed G2 as well."
    ),
    labels_dir: Path | None = LABELS_DIR_OPT,
    tiles_index: Path | None = TILES_INDEX_OPT,
) -> None:
    """Check the labels of a split and compute G2 -> reports/annotation.json; exit 1 when rejected."""
    from geoqc.annotation.validate import format_summary, validate_split

    cfg, paths = _paths(labels_dir, tiles_index)
    try:
        rep, code = validate_split(
            cfg,
            paths,
            _split(split),
            labels_path=labels,
            recheck_path=recheck,
            report_path=report,
            strict=strict,
        )
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    typer.echo(format_summary(rep, split))
    typer.echo(f"-> {report or paths.reports_dir / 'annotation.json'}")
    if code:
        typer.secho(
            "labels NOT accepted" if code == 1 else "accepted with warnings (strict)",
            fg="red" if code == 1 else "yellow",
        )
    else:
        typer.secho("labels accepted", fg="green")
    raise typer.Exit(code)
