"""Standalone PyQGIS script: build a ``.qgz`` project from a JSON layer spec.

Runs under QGIS' bundled Python (no ``geoqc`` imports). Invoked by ``geoqc.annotation.qgis_project``:

    python3 _qgis_build_project.py <spec.json> <out.qgz>

Spec (bottom -> top layer order)::

    {"title": str, "project_crs": "EPSG:326xx", "prefix_path": str, "extent_wgs84": [minx, miny, maxx, maxy],
     "avoid_intersections": ["buildings"],
     "layers": [{"type": "raster"|"xyz"|"vector", "name": str, "source": str, "style": str|null,
                 "visible": bool, "attribution": str|null, "label_field": str|null,
                 "form": {"defaults": {field: expr}, "widgets": {field: {"type": str, "config": {}}}}}]}

Prints one JSON line with the result; exit code 1 when the project could not be written.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _first(*getters):
    """Return the first attribute lookup that works (QGIS enum names moved between 3.2x and 3.4x)."""
    for g in getters:
        try:
            return g()
        except AttributeError:
            continue
    raise AttributeError("none of the enum candidates exist in this QGIS version")


def _configure_labels(layer, field: str) -> None:
    from qgis.core import (
        Qgis,
        QgsPalLayerSettings,
        QgsTextBufferSettings,
        QgsTextFormat,
        QgsVectorLayerSimpleLabeling,
    )
    from qgis.PyQt.QtGui import QColor

    settings = QgsPalLayerSettings()
    settings.fieldName = field
    settings.placement = _first(
        lambda: Qgis.LabelPlacement.Horizontal, lambda: QgsPalLayerSettings.Horizontal
    )
    fmt = QgsTextFormat()
    fmt.setSize(11)
    fmt.setColor(QColor(255, 212, 0))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(1.2)
    buf.setColor(QColor(0, 0, 0))
    fmt.setBuffer(buf)
    settings.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)


def _configure_form(layer, form: dict[str, Any]) -> list[str]:
    from qgis.core import QgsDefaultValue, QgsEditorWidgetSetup

    warnings: list[str] = []
    fields = layer.fields()
    for name, expr in (form.get("defaults") or {}).items():
        idx = fields.indexOf(name)
        if idx < 0:
            warnings.append(f"field {name!r} not found for default value")
            continue
        layer.setDefaultValueDefinition(idx, QgsDefaultValue(expr, False))
    for name, widget in (form.get("widgets") or {}).items():
        idx = fields.indexOf(name)
        if idx < 0:
            continue
        layer.setEditorWidgetSetup(
            idx, QgsEditorWidgetSetup(widget["type"], widget.get("config") or {})
        )
    cfg = layer.editFormConfig()
    for name in form.get("readonly") or []:
        idx = fields.indexOf(name)
        if idx >= 0:
            cfg.setReadOnly(idx, True)
    layer.setEditFormConfig(cfg)
    return warnings


def _set_attribution(layer, text: str) -> None:
    try:
        layer.serverProperties().setAttribution(text)
    except AttributeError:
        layer.setAttribution(text)


def main(argv: list[str]) -> int:
    spec_path, out_path = argv[1], argv[2]
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from qgis.core import (
        Qgis,
        QgsApplication,
        QgsCoordinateReferenceSystem,
        QgsProject,
        QgsRasterLayer,
        QgsRectangle,
        QgsReferencedRectangle,
        QgsSnappingConfig,
        QgsVectorLayer,
    )

    QgsApplication.setPrefixPath(spec.get("prefix_path", ""), True)
    app = QgsApplication([], False)
    app.initQgis()
    result: dict[str, Any] = {"qgis_version": Qgis.QGIS_VERSION, "layers": [], "warnings": []}
    try:
        project = QgsProject.instance()
        project.clear()
        project.setTitle(spec.get("title", "geoqc annotation"))
        project.setCrs(QgsCoordinateReferenceSystem(spec["project_crs"]))
        root = project.layerTreeRoot()
        by_name: dict[str, Any] = {}

        for spec_layer in spec["layers"]:
            kind = spec_layer["type"]
            name = spec_layer["name"]
            if kind == "raster":
                layer = QgsRasterLayer(spec_layer["source"], name)
            elif kind == "xyz":
                layer = QgsRasterLayer(spec_layer["source"], name, "wms")
            else:
                layer = QgsVectorLayer(spec_layer["source"], name, "ogr")
            info: dict[str, Any] = {"name": name, "type": kind, "valid": bool(layer.isValid())}
            if not layer.isValid():
                result["warnings"].append(f"layer {name!r} is invalid: {spec_layer['source']}")
                result["layers"].append(info)
                continue
            project.addMapLayer(layer, False)
            node = root.insertLayer(0, layer)
            node.setItemVisibilityChecked(bool(spec_layer.get("visible", True)))
            style = spec_layer.get("style")
            if style and os.path.exists(style):
                msg, ok_style = layer.loadNamedStyle(style)
                if not ok_style:
                    result["warnings"].append(f"style for {name!r} not loaded: {msg}")
            if spec_layer.get("attribution"):
                _set_attribution(layer, spec_layer["attribution"])
            if kind == "vector":
                info["renderer"] = layer.renderer().type() if layer.renderer() else None
                info["fields"] = [f.name() for f in layer.fields()]
                if spec_layer.get("label_field"):
                    _configure_labels(layer, spec_layer["label_field"])
                if spec_layer.get("form"):
                    result["warnings"].extend(_configure_form(layer, spec_layer["form"]))
            by_name[name] = layer
            result["layers"].append(info)

        snap = QgsSnappingConfig(project)
        snap.setEnabled(True)
        snap.setMode(
            _first(lambda: Qgis.SnappingMode.AllLayers, lambda: QgsSnappingConfig.AllLayers)
        )
        vertex = _first(lambda: Qgis.SnappingType.Vertex, lambda: QgsSnappingConfig.Vertex)
        segment = _first(lambda: Qgis.SnappingType.Segment, lambda: QgsSnappingConfig.Segment)
        try:
            snap.setTypeFlag(vertex | segment)
        except (AttributeError, TypeError):
            snap.setType(vertex)
        snap.setTolerance(10)
        snap.setUnits(_first(lambda: Qgis.MapToolUnit.Pixels, lambda: QgsSnappingConfig.Pixels))
        snap.setIntersectionSnapping(True)
        project.setSnappingConfig(snap)
        project.setTopologicalEditing(True)

        # "Current layer" mode: buildings avoid buildings, recheck avoids recheck. A layer list would
        # clip a blind recheck polygon by the hidden buildings layer and reveal the first labeling.
        if any(n in by_name for n in spec.get("avoid_intersections", [])):
            mode = _first(
                lambda: Qgis.AvoidIntersectionsMode.AvoidIntersectionsCurrentLayer,
                lambda: QgsProject.AvoidIntersectionsMode.AvoidIntersectionsCurrentLayer,
            )
            project.setAvoidIntersectionsMode(mode)

        extent = spec.get("extent_wgs84")
        if extent:
            rect = QgsReferencedRectangle(
                QgsRectangle(*extent), QgsCoordinateReferenceSystem("EPSG:4326")
            )
            project.viewSettings().setDefaultViewExtent(rect)
        project.writeEntry("Paths", "/Absolute", False)
        written = bool(project.write(out_path))
        result["written"] = written
        result["path"] = out_path
    except Exception as exc:  # report and fail loudly; caller falls back to manual instructions
        result["written"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        app.exitQgis()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("written") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
