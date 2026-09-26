"""Tests for the QGIS workspace (qgis_project.py) — with and without a QGIS installation."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
from typer.testing import CliRunner

from geoqc.annotation.cli import app
from geoqc.annotation.common import LABEL_PROPERTIES
from geoqc.annotation.qgis_project import (
    build_workspace,
    create_work_gpkg,
    esri_xyz_uri,
    fallback_instructions,
    find_qgis_python,
    write_styles,
)


def test_styles_are_valid_xml(tmp_path: Path):
    """Все .qml валидный XML с renderer-v2 и полигональным типом геометрии."""
    styles = write_styles(tmp_path)
    assert set(styles) == {"buildings", "recheck", "tiles_frame", "osm", "secondary"}
    for p in styles.values():
        root = ET.parse(p).getroot()
        assert root.tag == "qgis" and root.find("renderer-v2") is not None
        assert root.find("layerGeometryType").text == "2"
    rules = ET.parse(styles["buildings"]).getroot().find("renderer-v2")
    assert rules.get("type") == "RuleRenderer" and len(rules.find("rules")) == 2


def test_esri_uri_only_with_key():
    """XYZ-URI Esri: фигурные скобки закодированы, ключ подставлен, тип xyz."""
    uri = esri_xyz_uri("https://host/tile/{z}/{y}/{x}?token={key}", "SECRET")
    assert uri.startswith("type=xyz&url=https://host/tile/%7Bz%7D/%7By%7D/%7Bx%7D?token=SECRET")
    assert "zmax=19" in uri


def test_create_work_gpkg_from_draft(paths, drafted):
    """work.gpkg: слой buildings = копия черновика, слой recheck пустой, поля контракта, повторное создание защищено."""
    sdir = paths.split_dir("val")
    info = create_work_gpkg(sdir / "work.gpkg", sdir / "draft.geojson")
    assert info["created"] and info["n_buildings"] == drafted["val"]["n_draft_features"]
    layers = {n: g for n, g in pyogrio.list_layers(sdir / "work.gpkg")}
    assert set(layers) == {"buildings", "recheck"}
    b = gpd.read_file(sdir / "work.gpkg", layer="buildings")
    r = gpd.read_file(sdir / "work.gpkg", layer="recheck")
    assert set(LABEL_PROPERTIES) <= set(b.columns) and set(LABEL_PROPERTIES) <= set(r.columns)
    assert (
        len(r) == 0
        and pyogrio.read_info(sdir / "work.gpkg", layer="recheck")["geometry_type"]
        == "MultiPolygon"
    )
    assert b["verified"].dtype == bool and not b["verified"].any()
    again = create_work_gpkg(sdir / "work.gpkg", sdir / "draft.geojson")
    assert again["created"] is False
    forced = create_work_gpkg(sdir / "work.gpkg", sdir / "draft.geojson", force=True)
    assert forced["created"] and (sdir / "work.gpkg.bak").exists()


def test_create_work_gpkg_without_draft(tmp_path: Path):
    """Без черновика создаётся пустой слой buildings с MultiPolygon и полями контракта."""
    info = create_work_gpkg(tmp_path / "work.gpkg", None)
    assert info["n_buildings"] == 0
    assert (
        pyogrio.read_info(tmp_path / "work.gpkg", layer="buildings")["geometry_type"]
        == "MultiPolygon"
    )


def test_workspace_without_qgis(cfg, paths, drafted):
    """build_workspace(run_qgis=False): рамки тайлов с флагом recheck, стили, work.gpkg, layers.json, без .qgz."""
    cfg["annotation"]["qgis"]["osm_path"] = str(paths.interim_dir / "osm_buildings.geojson")
    cfg["annotation"]["qgis"]["secondary_path"] = str(
        paths.interim_dir / "secondary_buildings.geojson"
    )
    cfg["annotation"]["qgis"]["cog_path"] = str(paths.interim_dir / "aoi_cog.tif")
    rep = build_workspace(cfg, paths, "val", run_qgis=False, esri_key="")
    assert rep["project"] is None and rep["esri_layer"] is False
    frame = gpd.read_file(rep["tiles_frame"])
    rc = pd.read_csv(paths.recheck_tiles_csv, dtype=str)
    assert set(frame.loc[frame["recheck"].astype(bool), "tile_id"]) == set(
        rc.loc[rc["split"] == "val", "tile_id"]
    )
    assert rep["work_gpkg"]["created"]
    spec = json.loads(Path(rep["layer_spec"]).read_text())
    names = [layer["name"] for layer in spec["layers"]]
    assert names == [
        "osm_buildings",
        "secondary_buildings",
        "tiles_frame",
        "buildings",
        "recheck",
    ] or names[-3:] == ["tiles_frame", "buildings", "recheck"]
    assert "aoi_cog" in rep["layers_skipped_missing"]  # no COG in the synthetic world
    assert spec["project_crs"] == "EPSG:32637"
    text = fallback_instructions(rep)
    assert "work.gpkg" in text and "buildings.qml" in text


def test_workspace_with_esri_key_adds_xyz_layer(cfg, paths, drafted):
    """С ключом ARCGIS в спецификации появляется XYZ-слой Esri (скрытый), ключ не пишется в summary."""
    rep = build_workspace(cfg, paths, "test", run_qgis=False, esri_key="K123")
    spec = json.loads(Path(rep["layer_spec"]).read_text())
    xyz = [layer for layer in spec["layers"] if layer["type"] == "xyz"]
    assert len(xyz) == 1 and xyz[0]["visible"] is False and "token=K123" in xyz[0]["source"]
    assert rep["esri_layer"] is True
    assert "K123" not in json.dumps(rep)


@pytest.mark.slow
def test_pyqgis_project_generation(cfg, paths, drafted):
    """Headless PyQGIS (если QGIS.app установлен): .qgz создан, содержит слои, стили применены, форма настроена."""
    qgis_app = Path(cfg["annotation"]["qgis"].get("qgis_app", "/Applications/QGIS.app"))
    if find_qgis_python(qgis_app) is None:
        pytest.skip("QGIS.app with bundled Python not found")
    cfg["annotation"]["qgis"]["osm_path"] = str(paths.interim_dir / "osm_buildings.geojson")
    rep = build_workspace(cfg, paths, "val", run_qgis=True, esri_key="", annotator="student")
    res = rep["qgis_result"]
    assert rep["project"], res
    valid = {layer["name"]: layer for layer in res["layers"] if layer["valid"]}
    assert {"osm_buildings", "tiles_frame", "buildings", "recheck"} <= set(valid)
    assert valid["buildings"]["renderer"] == "RuleRenderer"
    assert valid["osm_buildings"]["renderer"] == "singleSymbol"
    assert set(LABEL_PROPERTIES) <= set(valid["buildings"]["fields"])
    with zipfile.ZipFile(rep["project"]) as zf:
        qgs = next(n for n in zf.namelist() if n.endswith(".qgs"))
        xml = zf.read(qgs).decode("utf-8")
    for name in ("buildings", "recheck", "tiles_frame", "osm_buildings"):
        assert f"<layername>{name}</layername>" in xml
    assert "aggregate('tiles_frame'" in xml  # default value for tile_id
    assert "student" in xml
    assert "<avoidIntersectionsMode>" in xml or "AvoidIntersections" in xml
    assert not [w for w in res["warnings"] if "style" in w], res["warnings"]


def test_cli_qgis_project_no_qgis(cli_env, paths, drafted):
    """CLI qgis-project --no-qgis печатает fallback-инструкцию и создаёт work.gpkg."""
    res = CliRunner().invoke(
        app,
        [
            "qgis-project",
            "--split",
            "val",
            "--no-qgis",
            "--labels-dir",
            str(paths.labels_dir),
            "--tiles-index",
            str(paths.tiles_index),
        ],
    )
    assert res.exit_code == 0, res.output
    assert (paths.split_dir("val") / "work.gpkg").exists()
    assert "Проект QGIS не создан автоматически" in res.output
    assert "skipped (no ARCGIS_API_KEY)" in res.output or "added (ARCGIS_API_KEY set)" in res.output
