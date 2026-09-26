"""``geoqc data`` commands (docs/CONTRACTS.md §6). Each step skips finished artifacts unless ``--force``."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import typer

from geoqc.config import load_config, resolve_path
from geoqc.data.manifest import load_manifest, update_manifest

app = typer.Typer(
    no_args_is_help=True, help="AOI search, imagery, tiles, OSM/secondary footprints, PostGIS load"
)
log = logging.getLogger("geoqc.data")


def _setup_logging(verbose: bool = True) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("rasterio").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _cfg() -> dict[str, Any]:
    cfg = load_config()
    if "data" not in cfg:
        raise typer.BadParameter("config/data.yaml is missing")
    return cfg


def _aoi(cfg: dict[str, Any]):
    from geoqc.data.aoi import load_aoi

    path = resolve_path(cfg["data"]["aoi"]["geojson"])
    if not path.exists():
        raise typer.Exit(code=_fail(f"{path} not found: run `geoqc data search-aoi` first"))
    return load_aoi(path)


def _fail(msg: str) -> int:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    return 1


def _image_meta(cfg: dict[str, Any], aoi_props: dict[str, Any]) -> dict[str, Any]:
    """Image metadata: ``config/aoi.geojson`` properties override ``config/data.yaml -> image``."""
    meta = dict(cfg["data"]["image"])
    mapping = {
        "image_id": "id",
        "image_url": "url",
        "source": "source",
        "acquired": "acquired",
        "license": "license",
        "attribution": "attribution",
        "native_gsd_m": "native_gsd_m",
        "meta_url": "meta_url",
    }
    for src_key, dst_key in mapping.items():
        if aoi_props.get(src_key) not in (None, ""):
            meta[dst_key] = aoi_props[src_key]
    return meta


def _raw_path(cfg: dict[str, Any], meta: dict[str, Any], user_input: Path | None) -> Path:
    if user_input is not None:
        return user_input
    return resolve_path(
        cfg["data"]["image"].get("raw_path") or f"data/raw/{meta['source']}_{meta['id']}.tif"
    )


# --------------------------------------------------------------------------- search-aoi
@app.command("search-aoi")
def search_aoi_cmd(
    skip_global: bool = typer.Option(False, help="Only the Russia bbox query (fast smoke run)"),
    write_aoi: bool = typer.Option(True, help="Write config/aoi.geojson for the chosen candidate"),
    choose: str | None = typer.Option(
        None, help="Force this OAM _id instead of the top-ranked candidate"
    ),
    verbose: bool = True,
) -> None:
    """Query OpenAerialMap (Russia first, then globally), rank candidates -> reports/aoi_candidates.json."""
    _setup_logging(verbose)
    from geoqc.data import aoi as aoi_mod

    cfg = _cfg()
    d = cfg["data"]
    crit = dict(d["search"])
    crit["min_aoi_area_km2"] = d["aoi"]["min_area_km2"]
    crit["max_aoi_area_km2"] = d["aoi"]["max_area_km2"]
    crit["erode_m"] = d["aoi"]["erode_m"]
    crit["simplify_m"] = d["aoi"]["simplify_m"]
    report = aoi_mod.search_aoi(
        crit,
        overpass_endpoints=d["overpass"]["endpoints"],
        overpass_timeout_s=d["overpass"]["timeout_s"],
        user_agent=d["overpass"]["user_agent"],
        report_path=resolve_path("reports/aoi_candidates.json"),
        skip_global=skip_global,
        force_choice=choose,
    )
    chosen = report.get("chosen")
    typer.echo(
        f"candidates passing filters: {len(report['candidates'])}; report -> reports/aoi_candidates.json"
    )
    if not chosen:
        typer.echo("No OAM candidate passed all checks; use Maxar Open Data fallback (see README).")
        return
    typer.echo(
        f"chosen: {chosen['title']} ({chosen['oam_id']}), {chosen['acquired']}, gsd {chosen['gsd_m']} m, {chosen['license']}"
    )
    if write_aoi:
        aoi_wgs, _aoi_native, epsg = aoi_mod.valid_footprint_polygon(
            chosen["url"],
            erode_m=d["aoi"]["erode_m"],
            simplify_m=d["aoi"]["simplify_m"],
            max_area_km2=d["aoi"]["max_area_km2"],
        )
        area = aoi_mod.aoi_area_km2(aoi_wgs, epsg)
        out = aoi_mod.write_aoi_geojson(
            aoi_wgs,
            resolve_path(d["aoi"]["geojson"]),
            name=chosen["title"],
            source="openaerialmap",
            image_id=chosen["oam_id"],
            utm_epsg=epsg,
            extra={
                "image_url": chosen["url"],
                "meta_url": f"{d['search']['oam_api']}/{chosen['oam_id']}",
                "acquired": chosen["acquired"],
                "license": chosen["license"],
                "attribution": f"{chosen['provider']} via OpenAerialMap, {chosen['license']}",
                "provider": chosen["provider"],
                "native_gsd_m": chosen.get("native_gsd_m") or chosen.get("gsd_m"),
                "area_km2": round(area, 3),
                "osm_buildings_in_bbox": chosen.get("osm_buildings"),
            },
        )
        typer.echo(f"AOI polygon {area:.2f} km2, UTM EPSG:{epsg} -> {out}")


# --------------------------------------------------------------------------- download
@app.command()
def download(force: bool = False, verbose: bool = True) -> None:
    """Fetch the source image into data/raw (whole file or AOI window via /vsicurl/), sha256 -> manifest."""
    _setup_logging(verbose)
    from geoqc.data.aoi import aoi_area_km2, aoi_bbox
    from geoqc.data.download import fetch_source_image

    cfg = _cfg()
    aoi_wgs, props = _aoi(cfg)
    meta = _image_meta(cfg, props)
    raw = _raw_path(cfg, meta, None)
    info = fetch_source_image(
        meta["url"],
        raw,
        aoi_wgs84=aoi_wgs,
        windowed_over_mb=cfg["data"]["image"]["windowed_over_mb"],
        force=force,
    )
    epsg = int(props.get("utm_epsg") or 0) or None
    update_manifest(
        "aoi",
        {
            "name": props.get("name"),
            "area_km2": round(aoi_area_km2(aoi_wgs, epsg), 3) if epsg else None,
            "utm_epsg": epsg,
            "bbox_wgs84": list(aoi_bbox(aoi_wgs)),
        },
    )
    update_manifest(
        "image",
        {
            "source": meta["source"],
            "id": str(meta["id"]),
            "url": meta["url"],
            "acquired": meta.get("acquired"),
            "license": meta.get("license"),
            "attribution": meta.get("attribution"),
            "sha256": info["sha256"],
            "gsd_m": info["gsd_m"],
            "crs": info["crs"],
            "raw_path": info["path"],
            "raw_size_bytes": info["size_bytes"],
            "download_mode": info["mode"],
        },
    )
    typer.echo(
        f"raw image: {raw} ({info['size_bytes'] / 1e6:.1f} MB, {info['mode']}, sha256 {info['sha256'][:12]}...)"
    )


# --------------------------------------------------------------------------- preprocess
@app.command()
def preprocess(
    input: Path | None = typer.Option(
        None, "--input", help="User GeoTIFF instead of the downloaded raw image"
    ),
    shift: str | None = typer.Option(
        None, help="Georeference shift 'dx,dy' in metres (east, north) applied to the image"
    ),
    gsd: float | None = typer.Option(None, help="Override preprocess.target_gsd_m"),
    force: bool = False,
    verbose: bool = True,
) -> None:
    """Reproject to UTM, clip by AOI, RGB uint8, COG -> data/interim/aoi_cog.tif; set pipeline.yaml crs.metric."""
    _setup_logging(verbose)
    from geoqc.data.crs import utm_epsg_for_bbox
    from geoqc.data.download import image_info
    from geoqc.data.manifest import sha256_of
    from geoqc.data.preprocess import build_aoi_cog, set_pipeline_metric_crs

    cfg = _cfg()
    d = cfg["data"]
    aoi_wgs, props = _aoi(cfg)
    meta = _image_meta(cfg, props)
    src = _raw_path(cfg, meta, input)
    if not src.exists():
        raise typer.Exit(code=_fail(f"{src} not found: run `geoqc data download` or pass --input"))
    out = resolve_path(d["preprocess"]["output"])
    epsg = int(props.get("utm_epsg") or utm_epsg_for_bbox(aoi_wgs.bounds))
    target = float(gsd or d["preprocess"]["target_gsd_m"])
    if target > float(cfg["image"]["max_gsd_m"]):
        raise typer.Exit(
            code=_fail(f"target GSD {target} > contract max {cfg['image']['max_gsd_m']}")
        )
    shift_m = (0.0, 0.0)
    if shift:
        dx, dy = (float(v) for v in shift.split(","))
        shift_m = (dx, dy)
    if out.exists() and not force and not shift:
        typer.echo(f"{out} exists, skipping (use --force)")
    else:
        res = build_aoi_cog(
            src,
            out,
            aoi_wgs84=aoi_wgs,
            dst_epsg=epsg,
            target_gsd_m=target,
            nodata=int(d["preprocess"]["nodata"]),
            resampling=d["preprocess"]["resampling"],
            shift_m=shift_m,
            block_px=int(d["preprocess"]["block_px"]),
            cog_profile=d["preprocess"]["cog_profile"],
        )
        typer.echo(
            f"COG: {out} {res['width']}x{res['height']} @ {target} m, EPSG:{epsg}, shift {res['shift_applied_m']}"
        )
        update_manifest("georef", {"shift_applied_m": res["shift_applied_m"]})
    set_pipeline_metric_crs(epsg)
    m = load_manifest()
    if input is not None:
        info = image_info(src)
        update_manifest(
            "image",
            {
                "source": "user",
                "id": src.stem,
                "url": str(src),
                "sha256": sha256_of(src),
                "gsd_m": info["gsd_m"],
                "crs": info["crs"],
                "raw_path": str(src),
            },
        )
    update_manifest(
        "aoi",
        {
            "name": m["aoi"].get("name") or props.get("name"),
            "utm_epsg": epsg,
            "bbox_wgs84": m["aoi"].get("bbox_wgs84") or [round(v, 7) for v in aoi_wgs.bounds],
        },
    )
    update_manifest("interim", {"aoi_cog": str(out), "target_gsd_m": target, "crs": f"EPSG:{epsg}"})
    typer.echo(f"config/pipeline.yaml crs.metric = EPSG:{epsg}")


# --------------------------------------------------------------------------- tile
@app.command()
def tile(force: bool = False, verbose: bool = True) -> None:
    """Cut data/interim/aoi_cog.tif into 512x512 tiles with 64 px overlap and build tiles.gpkg."""
    _setup_logging(verbose)
    import rasterio

    from geoqc.data.tiling import aoi_to_crs, tile_raster

    cfg = _cfg()
    d = cfg["data"]
    aoi_wgs, _props = _aoi(cfg)
    cog = resolve_path(d["preprocess"]["output"])
    if not cog.exists():
        raise typer.Exit(code=_fail(f"{cog} not found: run `geoqc data preprocess`"))
    out_dir = resolve_path(cfg["paths"]["tiles"])
    index = resolve_path(cfg["tiles"]["index"])
    if index.exists() and not force and any(out_dir.glob("*.tif")):
        typer.echo(f"{index} exists, skipping (use --force)")
        return
    if force:
        for p in out_dir.glob("*.tif"):
            p.unlink()
    with rasterio.open(cog) as ds:
        aoi_native = (
            aoi_to_crs(aoi_wgs, ds.crs) if d["tiles"]["keep_only_intersecting_aoi"] else None
        )
    res = tile_raster(
        cog,
        out_dir,
        index,
        size_px=int(cfg["tiles"]["size_px"]),
        overlap_px=int(cfg["tiles"]["overlap_px"]),
        nodata=int(d["preprocess"]["nodata"]),
        aoi_geom=aoi_native,
        layer=d["tiles"]["layer"],
        name_pattern=cfg["tiles"]["name_pattern"],
        force=force,
    )
    update_manifest("tiles", res)
    typer.echo(json.dumps(res, indent=1))


# --------------------------------------------------------------------------- osm
@app.command()
def osm(force: bool = False, verbose: bool = True) -> None:
    """Overpass buildings (ways + multipolygon relations) -> data/interim/osm_buildings.geojson."""
    _setup_logging(verbose)
    from geoqc.data.aoi import aoi_bbox
    from geoqc.data.osm import download_osm_buildings

    cfg = _cfg()
    d = cfg["data"]
    aoi_wgs, _props = _aoi(cfg)
    out = resolve_path(d["osm"]["output"])
    if out.exists() and not force:
        typer.echo(f"{out} exists, skipping (use --force)")
        return
    res = download_osm_buildings(
        aoi_bbox(aoi_wgs),
        cache_dir=resolve_path(d["osm"]["cache_dir"]),
        output=out,
        endpoints=d["overpass"]["endpoints"],
        query_template=d["osm"]["query"],
        bbox_buffer_m=float(d["osm"]["bbox_buffer_m"]),
        min_area_m2=float(d["osm"]["min_area_m2"]),
        timeout_s=int(d["overpass"]["timeout_s"]),
        retries=int(d["overpass"]["retries"]),
        backoff_s=float(d["overpass"]["backoff_s"]),
        user_agent=d["overpass"]["user_agent"],
        force=force,
    )
    update_manifest(
        "osm",
        {
            "snapshot_date": res["snapshot_date"],
            "query": res["query"],
            "count": res["count"],
            "license": "ODbL",
            "cache": res["cache"],
        },
    )
    typer.echo(f"OSM buildings: {res['count']} (snapshot {res['snapshot_date']}) -> {out}")


# --------------------------------------------------------------------------- secondary
@app.command()
def secondary(force: bool = False, verbose: bool = True) -> None:
    """Microsoft GlobalMLBuildingFootprints or Google Open Buildings for the AOI (source=null when absent)."""
    _setup_logging(verbose)
    from geoqc.data.aoi import aoi_bbox
    from geoqc.data.secondary import fetch_secondary

    cfg = _cfg()
    d = cfg["data"]
    aoi_wgs, _props = _aoi(cfg)
    out = resolve_path(d["secondary"]["output"])
    if out.exists() and not force:
        typer.echo(f"{out} exists, skipping (use --force)")
        return
    res = fetch_secondary(
        aoi_bbox(aoi_wgs),
        output=out,
        microsoft_links_csv=d["secondary"]["microsoft_links_csv"],
        microsoft_level=int(d["secondary"]["microsoft_quadkey_level"]),
        google_tiles_url=d["secondary"]["google_open_buildings_tiles"],
        google_base_url=d["secondary"]["google_open_buildings_base"],
    )
    update_manifest("secondary", {k: v for k, v in res.items() if k != "output"})
    typer.echo(f"secondary: source={res['source']} count={res['count']} -> {out}")


# --------------------------------------------------------------------------- load-ref
@app.command("load-ref")
def load_ref(
    source: str = typer.Option("all", help="osm | secondary | all"),
    test_db: bool = typer.Option(False, help="Load into TEST_DATABASE_URL instead of DATABASE_URL"),
    verbose: bool = True,
) -> None:
    """Load OSM (and secondary) footprints into buildings_ref; idempotent (DELETE WHERE source=...)."""
    _setup_logging(verbose)
    from geoqc.data.loadref import count_by_source, load_geojson_file
    from geoqc.db import apply_schema, connect

    cfg = _cfg()
    d = cfg["data"]
    batch = int(d["load_ref"]["batch_size"])
    counts: dict[str, int] = {}
    with connect(test=test_db) as conn:
        apply_schema(conn)
        conn.commit()
        if source in ("osm", "all"):
            osm_path = resolve_path(d["osm"]["output"])
            if not osm_path.exists():
                raise typer.Exit(code=_fail(f"{osm_path} not found: run `geoqc data osm`"))
            counts["osm"] = load_geojson_file(conn, osm_path, source="osm", batch_size=batch)
        if source in ("secondary", "all"):
            sec_path = resolve_path(d["secondary"]["output"])
            if sec_path.exists():
                with sec_path.open("r", encoding="utf-8") as fh:
                    sec_src = (json.load(fh).get("secondary") or {}).get("source")
                if sec_src in ("microsoft", "google"):
                    counts[sec_src] = load_geojson_file(
                        conn, sec_path, source=sec_src, batch_size=batch
                    )
                else:
                    typer.echo("secondary source is null; nothing to load")
        total = count_by_source(conn)
    update_manifest("buildings_ref", {"loaded": counts, "count_by_source": total})
    typer.echo(f"buildings_ref: {total}")


# --------------------------------------------------------------------------- check-georef
@app.command("check-georef")
def check_georef_cmd(verbose: bool = True) -> None:
    """Estimate image-vs-OSM offset on >= 10 control buildings -> reports/georef.json (gate G1)."""
    _setup_logging(verbose)
    from geoqc.data.georef import check_georef

    cfg = _cfg()
    d = cfg["data"]
    aoi_wgs, props = _aoi(cfg)
    cog = resolve_path(d["preprocess"]["output"])
    osm_path = resolve_path(d["osm"]["output"])
    for p in (cog, osm_path):
        if not p.exists():
            raise typer.Exit(code=_fail(f"{p} not found"))
    m = load_manifest()
    epsg = int(m["aoi"].get("utm_epsg") or props.get("utm_epsg"))
    rep = check_georef(
        cog,
        osm_path,
        aoi_wgs,
        d["georef"],
        report_path=resolve_path(d["georef"]["output"]),
        metric_epsg=epsg,
    )
    update_manifest(
        "georef",
        {
            "n_control_points": rep["n_control_points"],
            "median_offset_m": rep["median_offset_m"],
            "systematic_shift_m": rep["systematic_shift_m"],
            "shift_applied_m": m["georef"].get("shift_applied_m", [0.0, 0.0]),
            "gate_G1_passed": rep["gate_G1"]["passed"],
        },
    )
    typer.echo(
        f"georef: n={rep['n_control_points']} median={rep['median_offset_m']} m "
        f"systematic={rep['systematic_shift_m']} G1 passed={rep['gate_G1']['passed']} recommend_shift={rep['recommend_shift']}"
    )


# --------------------------------------------------------------------------- all
@app.command("all")
def run_all(
    force: bool = typer.Option(False, help="Rebuild every artifact"),
    skip_secondary: bool = False,
    skip_db: bool = False,
    verbose: bool = True,
) -> None:
    """download -> preprocess -> tile -> osm -> secondary -> load-ref -> check-georef (+ shift correction)."""
    _setup_logging(verbose)
    cfg = _cfg()
    d = cfg["data"]
    download(force=force, verbose=verbose)
    preprocess(input=None, shift=None, gsd=None, force=force, verbose=verbose)
    tile(force=force, verbose=verbose)
    osm(force=force, verbose=verbose)
    if not skip_secondary:
        secondary(force=force, verbose=verbose)
    if not skip_db:
        load_ref(source="all", test_db=False, verbose=verbose)
    check_georef_cmd(verbose=verbose)
    rep_path = resolve_path(d["georef"]["output"])
    with rep_path.open("r", encoding="utf-8") as fh:
        rep = json.load(fh)
    if rep.get("recommend_shift"):
        # Image content sits at (dx, dy) from the vector: move the image by (-dx, -dy) and re-run.
        dx, dy = rep["systematic_shift_m"]
        shift = f"{-dx:.3f},{-dy:.3f}"
        typer.echo(
            f"median offset {rep['median_offset_m']} m > gate: applying shift {shift} and re-tiling"
        )
        preprocess(input=None, shift=shift, gsd=None, force=True, verbose=verbose)
        tile(force=True, verbose=verbose)
        check_georef_cmd(verbose=verbose)
    typer.echo("geoqc data all: done")
