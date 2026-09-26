"""QGIS workspace for a split: working GeoPackage, tile frames, styles and (on macOS) a ``.qgz`` project.

Layout written to ``data/labels/<split>/``::

    work.gpkg                 layers `buildings` (copy of draft.geojson, editable) and `recheck` (empty)
    qgis/tiles_frame.geojson  tiles of the split (tile_id, stratum, recheck flag) for orientation + snapping
    qgis/*.qml                styles for every layer (fallback when the project cannot be generated)
    qgis/layers.json          layer spec consumed by _qgis_build_project.py
    qgis/annotation_<split>.qgz   the project (only when QGIS' bundled Python is available)

The project layers, top -> bottom: recheck (hidden), buildings, tiles_frame, osm_buildings (hint only),
secondary_buildings (hidden), Esri World Imagery XYZ (hidden, only with ARCGIS_API_KEY; D-002), aoi_cog.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import geopandas as gpd
import pandas as pd

from geoqc.annotation.common import (
    LABEL_PROPERTIES,
    STORAGE_CRS,
    Paths,
    check_split,
    metric_crs,
    read_tiles,
    read_tiles_split,
    read_vector,
    resolve_path,
    split_tiles,
    stable_json,
)
from geoqc.config import get_settings

SCRIPT = Path(__file__).with_name("_qgis_build_project.py")

# --------------------------------------------------------------------------- QML styles


def _fill_symbol(
    name: str,
    fill: str,
    outline: str,
    width: float,
    outline_style: str = "solid",
    fill_style: str = "solid",
) -> str:
    return f"""      <symbol type="fill" name="{name}" alpha="1" clip_to_extent="1" force_rhr="0" is_animated="0" frame_rate="10">
        <layer class="SimpleFill" enabled="1" locked="0" pass="0" id="{{{uuid.uuid4()}}}">
          <Option type="Map">
            <Option type="QString" name="border_width_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="color" value="{fill}"/>
            <Option type="QString" name="joinstyle" value="miter"/>
            <Option type="QString" name="offset" value="0,0"/>
            <Option type="QString" name="offset_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="offset_unit" value="MM"/>
            <Option type="QString" name="outline_color" value="{outline}"/>
            <Option type="QString" name="outline_style" value="{outline_style}"/>
            <Option type="QString" name="outline_width" value="{width}"/>
            <Option type="QString" name="outline_width_unit" value="MM"/>
            <Option type="QString" name="style" value="{fill_style}"/>
          </Option>
        </layer>
      </symbol>
"""


def _qml(renderer: str) -> str:
    return (
        "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
        '<qgis styleCategories="Symbology" version="3.34.0">\n'
        f"{renderer}"
        "  <layerGeometryType>2</layerGeometryType>\n"
        "</qgis>\n"
    )


def _single(symbol: str) -> str:
    return (
        '  <renderer-v2 type="singleSymbol" forceraster="0" symbollevels="0" enableorderby="0" referencescale="-1">\n'
        "    <symbols>\n"
        f"{symbol}"
        "    </symbols>\n"
        "  </renderer-v2>\n"
    )


def _rules(rules: list[tuple[str, str, str]]) -> str:
    """rules: (label, filter expression, symbol xml) in order; symbol names are 0..n-1."""
    rule_xml = "".join(
        f'      <rule key="{{{uuid.uuid4()}}}" symbol="{i}" label="{label}" filter="{_esc(filt)}"/>\n'
        for i, (label, filt, _) in enumerate(rules)
    )
    symbols = "".join(sym for _, _, sym in rules)
    return (
        '  <renderer-v2 type="RuleRenderer" forceraster="0" symbollevels="0" enableorderby="0" referencescale="-1">\n'
        f'    <rules key="{{{uuid.uuid4()}}}">\n{rule_xml}    </rules>\n'
        f"    <symbols>\n{symbols}    </symbols>\n"
        "  </renderer-v2>\n"
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def write_styles(qdir: Path) -> dict[str, Path]:
    """Write ``.qml`` styles; returns {layer role: path}."""
    qdir.mkdir(parents=True, exist_ok=True)
    styles = {
        "buildings": _qml(
            _rules(
                [
                    (
                        "не проверено (verified=false)",
                        'coalesce("verified", false) = false',
                        _fill_symbol("0", "255,140,0,70", "255,40,0,255", 0.5),
                    ),
                    (
                        "проверено (verified=true)",
                        '"verified" = true',
                        _fill_symbol("1", "0,200,80,30", "0,170,60,255", 0.5),
                    ),
                ]
            )
        ),
        "recheck": _qml(_single(_fill_symbol("0", "255,0,255,0", "255,0,220,255", 0.6, "dash"))),
        "tiles_frame": _qml(
            _rules(
                [
                    (
                        "тайл",
                        'coalesce("recheck", false) = false',
                        _fill_symbol("0", "255,255,0,0", "255,220,0,255", 0.9),
                    ),
                    (
                        "тайл для повторной разметки (recheck)",
                        '"recheck" = true',
                        _fill_symbol("1", "0,255,255,0", "0,220,255,255", 1.2),
                    ),
                ]
            )
        ),
        "osm": _qml(_single(_fill_symbol("0", "0,120,255,0", "0,120,255,255", 0.35, "dash"))),
        "secondary": _qml(
            _single(_fill_symbol("0", "128,128,128,0", "140,140,140,255", 0.35, "dot"))
        ),
    }
    out = {}
    for role, xml in styles.items():
        p = qdir / f"{role}.qml"
        p.write_text(xml, encoding="utf-8")
        out[role] = p
    return out


# --------------------------------------------------------------------------- working files


def empty_labels_frame() -> gpd.GeoDataFrame:
    frame = gpd.GeoDataFrame(
        {
            "tile_id": pd.Series(dtype="str"),
            "split": pd.Series(dtype="str"),
            "source": pd.Series(dtype="str"),
            "verified": pd.Series(dtype="bool"),
            "annotator": pd.Series(dtype="str"),
            "notes": pd.Series(dtype="str"),
        },
        geometry=gpd.GeoSeries([], crs=STORAGE_CRS),
        crs=STORAGE_CRS,
    )
    return frame


def _write_gpkg_layer(gdf: gpd.GeoDataFrame, path: Path, layer: str) -> None:
    gdf = gdf.to_crs(STORAGE_CRS)
    gdf.to_file(
        path,
        layer=layer,
        driver="GPKG",
        engine="pyogrio",
        geometry_type="MultiPolygon",
        promote_to_multi=True,
    )


def create_work_gpkg(path: Path, draft_path: Path | None, force: bool = False) -> dict[str, Any]:
    """Create ``work.gpkg`` with `buildings` (from draft) and `recheck` (empty); never overwrite silently."""
    if path.exists() and not force:
        return {
            "path": str(path),
            "created": False,
            "reason": "exists (use --force to recreate; a .bak copy is kept)",
        }
    if path.exists():
        shutil.copyfile(path, path.with_suffix(".gpkg.bak"))
        path.unlink()
    if draft_path is not None and draft_path.exists():
        draft = read_vector(draft_path)
        if len(draft) == 0:
            draft = empty_labels_frame()
    else:
        draft = empty_labels_frame()
    for prop in LABEL_PROPERTIES:
        if prop not in draft.columns:
            draft[prop] = False if prop == "verified" else ""
    draft["verified"] = draft["verified"].astype(bool)
    draft = gpd.GeoDataFrame(
        draft[[*LABEL_PROPERTIES, "geometry"]], geometry="geometry", crs=draft.crs
    )
    _write_gpkg_layer(draft, path, "buildings")
    _write_gpkg_layer(empty_labels_frame(), path, "recheck")
    return {"path": str(path), "created": True, "n_buildings": int(len(draft))}


def write_tiles_frame(tiles_s: gpd.GeoDataFrame, recheck_ids: set[str], path: Path) -> Path:
    frame = tiles_s[["tile_id", "stratum", "row", "col", "geometry"]].copy()
    frame["recheck"] = frame["tile_id"].isin(recheck_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame(frame, geometry="geometry", crs=tiles_s.crs).to_crs(STORAGE_CRS).to_file(
        path, driver="GeoJSON", engine="pyogrio"
    )
    return path


# --------------------------------------------------------------------------- PyQGIS runner


def find_qgis_python(qgis_app: Path) -> tuple[Path, dict[str, str]] | None:
    """Locate QGIS' bundled interpreter (macOS .app) and build the environment to run it headless."""
    qgis_app = Path(qgis_app)
    macos = qgis_app / "Contents" / "MacOS"
    resources = qgis_app / "Contents" / "Resources"
    if not macos.exists():
        return None
    exe = next((p for p in sorted(macos.glob("python3*")) if p.is_file()), None)
    if exe is None:
        return None
    stdlib = next(
        (
            p
            for p in sorted(resources.glob("python3*"))
            if (p / "site-packages" / "qgis" / "core").exists()
        ),
        None,
    )
    if stdlib is None:
        return None
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONHOME", "PYTHONPATH"}}
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                str(p) for p in (stdlib, stdlib / "lib-dynload", stdlib / "site-packages")
            ),
            "QT_QPA_PLATFORM": "offscreen",
            "QGIS_PREFIX_PATH": str(macos),
        }
    )
    proj = resources / "qgis" / "proj"
    gdal = resources / "qgis" / "gdal"
    if proj.exists():
        env["PROJ_LIB"] = str(proj)
        env["PROJ_DATA"] = str(proj)
    if gdal.exists():
        env["GDAL_DATA"] = str(gdal)
    return exe, env


def run_pyqgis(
    spec_path: Path, out_path: Path, exe: Path, env: dict[str, str], timeout: int = 300
) -> dict[str, Any]:
    proc = subprocess.run(
        [str(exe), str(SCRIPT), str(spec_path), str(out_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    result: dict[str, Any] = {"returncode": proc.returncode, "stderr_tail": proc.stderr[-2000:]}
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                result.update(json.loads(line))
                break
            except json.JSONDecodeError:
                continue
    result.setdefault("written", False)
    return result


# --------------------------------------------------------------------------- workspace


def esri_xyz_uri(url_template: str, key: str) -> str:
    url = url_template.replace("{key}", key)
    return f"type=xyz&url={quote(url, safe=':/?=&')}&zmax=19&zmin=0"


def build_layer_spec(
    cfg: dict[str, Any],
    split: str,
    sdir: Path,
    qdir: Path,
    styles: dict[str, Path],
    tiles_frame: Path,
    work_gpkg: Path,
    crs_metric: str,
    extent_wgs84: list[float],
    esri_key: str | None,
    annotator: str,
) -> dict[str, Any]:
    qcfg = cfg["annotation"].get("qgis", {})
    layers: list[dict[str, Any]] = []
    cog = resolve_path(qcfg.get("cog_path", "data/interim/aoi_cog.tif"))
    layers.append(
        {
            "type": "raster",
            "name": "aoi_cog",
            "source": str(cog),
            "style": None,
            "visible": True,
            "exists": cog.exists(),
        }
    )
    if esri_key:
        layers.append(
            {
                "type": "xyz",
                "name": "Esri World Imagery (reference only)",
                "source": esri_xyz_uri(qcfg["esri_url"], esri_key),
                "style": None,
                "visible": False,
                "attribution": qcfg.get("esri_attribution", "Esri"),
                "exists": True,
            }
        )
    secondary = resolve_path(qcfg.get("secondary_path", "data/interim/secondary_buildings.geojson"))
    if secondary.exists():
        layers.append(
            {
                "type": "vector",
                "name": "secondary_buildings",
                "source": str(secondary),
                "style": str(styles["secondary"]),
                "visible": False,
                "exists": True,
            }
        )
    osm = resolve_path(qcfg.get("osm_path", "data/interim/osm_buildings.geojson"))
    layers.append(
        {
            "type": "vector",
            "name": "osm_buildings",
            "source": str(osm),
            "style": str(styles["osm"]),
            "visible": True,
            "attribution": "© OpenStreetMap contributors (ODbL)",
            "exists": osm.exists(),
        }
    )
    layers.append(
        {
            "type": "vector",
            "name": "tiles_frame",
            "source": str(tiles_frame),
            "style": str(styles["tiles_frame"]),
            "visible": True,
            "label_field": "tile_id",
            "exists": True,
        }
    )
    form = {
        "defaults": {
            "tile_id": "coalesce(aggregate('tiles_frame', 'max', \"tile_id\", intersects($geometry, geometry(@parent))), '')",
            "split": f"'{split}'",
            "source": "'manual'",
            "verified": "false",
            "annotator": f"'{annotator}'",
            "notes": "''",
        },
        "widgets": {
            "verified": {
                "type": "CheckBox",
                "config": {"CheckedState": "true", "UncheckedState": "false"},
            },
            "source": {
                "type": "ValueMap",
                "config": {"map": [{"manual": "manual"}, {"pre_annotation": "pre_annotation"}]},
            },
            "split": {"type": "TextEdit", "config": {}},
        },
        "readonly": ["split"],
    }
    layers.append(
        {
            "type": "vector",
            "name": "buildings",
            "source": f"{work_gpkg}|layername=buildings",
            "style": str(styles["buildings"]),
            "visible": True,
            "form": form,
            "exists": True,
        }
    )
    layers.append(
        {
            "type": "vector",
            "name": "recheck",
            "source": f"{work_gpkg}|layername=recheck",
            "style": str(styles["recheck"]),
            "visible": False,
            "form": form,
            "exists": True,
        }
    )
    spec = {
        "title": f"geoqc annotation — {split}",
        "project_crs": crs_metric,
        "prefix_path": "",
        "extent_wgs84": extent_wgs84,
        "avoid_intersections": ["buildings", "recheck"],
        "layers": [layer for layer in layers if layer.get("exists", True)],
        "skipped_missing": [layer["name"] for layer in layers if not layer.get("exists", True)],
    }
    for layer in spec["layers"]:
        layer.pop("exists", None)
    return spec


def build_workspace(
    cfg: dict[str, Any],
    paths: Paths,
    split: str,
    force: bool = False,
    run_qgis: bool = True,
    esri_key: str | None = None,
    annotator: str = "",
) -> dict[str, Any]:
    """Create the QGIS working files for ``split`` and try to generate the ``.qgz`` project."""
    check_split(split)
    qcfg = cfg["annotation"].get("qgis", {})
    sdir = paths.split_dir(split)
    qdir = sdir / "qgis"
    qdir.mkdir(parents=True, exist_ok=True)

    tiles_all = read_tiles(paths.tiles_index)
    tiles_s = split_tiles(tiles_all, read_tiles_split(paths.tiles_split_csv), split)
    crs_m = metric_crs(cfg, tiles_all.geometry, paths.manifest)
    recheck_ids: set[str] = set()
    if paths.recheck_tiles_csv.exists():
        rc = pd.read_csv(paths.recheck_tiles_csv, dtype=str)
        recheck_ids = set(rc.loc[rc["split"] == split, "tile_id"])

    styles = write_styles(qdir)
    tiles_frame = write_tiles_frame(tiles_s, recheck_ids, qdir / "tiles_frame.geojson")
    draft_path = sdir / "draft.geojson"
    gpkg_info = create_work_gpkg(
        sdir / "work.gpkg", draft_path if draft_path.exists() else None, force
    )

    if esri_key is None:
        esri_key = get_settings().arcgis_api_key or ""
    minx, miny, maxx, maxy = (float(v) for v in tiles_s.to_crs(STORAGE_CRS).total_bounds)
    spec = build_layer_spec(
        cfg,
        split,
        sdir,
        qdir,
        styles,
        tiles_frame,
        sdir / "work.gpkg",
        crs_m,
        [minx, miny, maxx, maxy],
        esri_key or None,
        annotator,
    )
    skipped = spec.pop("skipped_missing")
    qgis_app = Path(qcfg.get("qgis_app", "/Applications/QGIS.app"))
    found = find_qgis_python(qgis_app)
    spec["prefix_path"] = str(qgis_app / "Contents" / "MacOS") if found else ""
    spec_path = qdir / "layers.json"
    spec_path.write_text(stable_json(spec, relative_paths=False), encoding="utf-8")

    summary: dict[str, Any] = {
        "split": split,
        "metric_crs": crs_m,
        "n_tiles": int(len(tiles_s)),
        "recheck_tiles": sorted(recheck_ids),
        "work_gpkg": gpkg_info,
        "tiles_frame": str(tiles_frame),
        "styles": {k: str(v) for k, v in styles.items()},
        "layer_spec": str(spec_path),
        "layers_skipped_missing": skipped,
        "esri_layer": bool(esri_key),
        "draft_present": draft_path.exists(),
        "project": None,
        "qgis_python": str(found[0]) if found else None,
        "qgis_result": None,
    }
    if run_qgis and found:
        out = qdir / f"annotation_{split}.qgz"
        res = run_pyqgis(spec_path, out, found[0], found[1])
        summary["qgis_result"] = res
        if res.get("written") and out.exists():
            summary["project"] = str(out)
    (qdir / "workspace_summary.json").write_text(stable_json(summary), encoding="utf-8")
    return summary


def fallback_instructions(summary: dict[str, Any]) -> str:
    """Manual steps when the ``.qgz`` could not be generated (also in docs/annotation/INSTRUCTION_QGIS.md)."""
    lines = [
        "Проект QGIS не создан автоматически. Собери его вручную (QGIS 3.28+):",
        "  1. Layer → Add Layer → Add Raster Layer: data/interim/aoi_cog.tif",
        f"  2. Layer → Add Layer → Add Vector Layer: {summary['tiles_frame']}  (стиль: qgis/tiles_frame.qml)",
        "  3. Add Vector Layer: data/interim/osm_buildings.geojson  (стиль: qgis/osm.qml, только подсказка)",
        f"  4. Add Vector Layer: {summary['work_gpkg']['path']}, слои `buildings` (стиль qgis/buildings.qml) и `recheck` (qgis/recheck.qml)",
        "  5. Стиль: правый клик по слою → Properties → Symbology → Style → Load Style → выбрать .qml",
        "  6. Project → Snapping Options: включить, All layers, Vertex+Segment, 10 px; Avoid overlap для слоя buildings",
        "  7. Project → Save As → data/labels/<split>/qgis/annotation_<split>.qgz",
    ]
    if sys.platform != "darwin":
        lines.append(
            "  (автогенерация проекта поддерживается только через /Applications/QGIS.app на macOS)"
        )
    return "\n".join(lines)
