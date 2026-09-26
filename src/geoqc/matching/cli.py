"""``geoqc matching`` commands: init-db, run, export, stats (docs/CONTRACTS.md §6)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from geoqc.config import load_config, resolve_path
from geoqc.db import apply_schema, connect

app = typer.Typer(no_args_is_help=True, help="Matching, discrepancy classification and scoring")

_TEST_DB = typer.Option(False, "--test-db", help="Use TEST_DATABASE_URL instead of DATABASE_URL")


@app.command("init-db")
def init_db(test_db: bool = _TEST_DB) -> None:
    """Apply every sql/NNN_*.sql (idempotent) via geoqc.db.apply_schema."""
    with connect(test=test_db) as conn:
        applied = apply_schema(conn)
    typer.echo("applied: " + ", ".join(applied))


@app.command()
def run(
    model_version: str | None = typer.Option(
        None, "--model-version", "-m", help="buildings_det.model_version (auto when only one)"
    ),
    algorithm: str | None = typer.Option(
        None, "--algorithm", "-a", help="greedy | hungarian (default: config/matching.yaml)"
    ),
    metric_srid: int | None = typer.Option(
        None, "--metric-srid", help="UTM EPSG code (default: config/pipeline.yaml crs.metric)"
    ),
    aoi: Path | None = typer.Option(None, "--aoi", help="AOI GeoJSON (default: matching.aoi_path)"),
    test_db: bool = _TEST_DB,
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
) -> None:
    """Fill `matches` and `discrepancies` for one model version (idempotent)."""
    from geoqc.matching.matcher import run_matching

    cfg = load_config()
    try:
        with connect(test=test_db) as conn:
            res = run_matching(
                conn,
                model_version,
                cfg=cfg,
                metric_srid=metric_srid,
                algorithm=algorithm,
                aoi_path=aoi,
            )
    except (RuntimeError, ValueError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        return
    typer.echo(
        f"model_version={res.model_version} metric_srid={res.metric_srid} algorithm={res.algorithm} "
        f"aoi={'yes' if res.aoi_used else 'no'} secondary={'yes' if res.has_secondary else 'no'}"
    )
    typer.echo(
        f"ref(osm)={res.n_ref_osm} ref(secondary)={res.n_ref_secondary} det={res.n_det} "
        f"split={res.n_det_split} suppressed={res.n_det_suppressed} candidates>=iou_match={res.n_candidates}"
    )
    mean_iou = f"{res.mean_iou_matched:.3f}" if res.mean_iou_matched is not None else "n/a"
    typer.echo(f"matches={res.n_matches} mean_iou_matched={mean_iou}")
    for cls, n in sorted(res.counts_by_class.items()):
        typer.echo(f"  {cls}: {n}")
    typer.echo(f"scored={res.n_scored} aoi_edge={res.n_aoi_edge}")


@app.command()
def export(
    out: Path | None = typer.Option(None, "--out", "-o", help="Default: matching.export.path"),
    model_version: str | None = typer.Option(None, "--model-version", "-m"),
    classes: str | None = typer.Option(None, "--class", help="Comma-separated class filter"),
    status: str | None = typer.Option(None, "--status", help="Comma-separated status filter"),
    min_conf: float = typer.Option(0.0, "--min-conf"),
    limit: int | None = typer.Option(None, "--limit"),
    test_db: bool = _TEST_DB,
) -> None:
    """Write reports/discrepancies.geojson (contract §3.8), sorted by confidence DESC."""
    from geoqc.matching.export import export_discrepancies

    mcfg = load_config()["matching"]
    out_path = out or resolve_path(
        mcfg.get("export", {}).get("path", "reports/discrepancies.geojson")
    )
    with connect(test=test_db) as conn:
        n = export_discrepancies(
            conn,
            out_path,
            model_version=model_version,
            classes=[c.strip() for c in classes.split(",")] if classes else None,
            statuses=[s.strip() for s in status.split(",")] if status else None,
            min_conf=min_conf,
            limit=limit,
            precision=int(mcfg.get("export", {}).get("precision", 7)),
        )
    typer.echo(f"wrote {n} features -> {out_path}")


@app.command()
def stats(
    model_version: str | None = typer.Option(None, "--model-version", "-m"),
    test_db: bool = _TEST_DB,
    as_json: bool = typer.Option(False, "--json", help="Print as JSON"),
) -> None:
    """Counters: reference / detections / matches / discrepancies by class and status."""
    from geoqc.matching.matcher import stats as _stats

    with connect(test=test_db) as conn:
        s = _stats(conn, model_version)
    if as_json:
        typer.echo(json.dumps(s, ensure_ascii=False, indent=2, default=str))
        return
    typer.echo(f"model_version: {s['model_version'] or '(all)'}  available: {s['model_versions']}")
    typer.echo(f"buildings_ref: {s['buildings_ref']}")
    typer.echo(f"buildings_det: {s['buildings_det']} (suppressed: {s['det_suppressed']})")
    mean_iou = f"{s['mean_iou_matched']:.3f}" if s["mean_iou_matched"] is not None else "n/a"
    typer.echo(f"matches: {s['matches']} mean_iou_matched: {mean_iou}")
    typer.echo(f"discrepancies: {s['discrepancies_total']} (aoi_edge: {s['aoi_edge']})")
    for cls, c in sorted(s["discrepancies"].items()):
        mc = f"{c['mean_confidence']:.3f}" if c["mean_confidence"] is not None else "n/a"
        typer.echo(f"  {cls}: {c['total']} {c['by_status']} mean_conf={mc}")
