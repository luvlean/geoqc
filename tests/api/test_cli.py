"""CLI ``geoqc api``: наличие команды serve с нужными опциями и импортируемость ASGI-приложения."""

from __future__ import annotations

import importlib

from typer.testing import CliRunner

from geoqc.cli import app as root_app


def test_serve_help_lists_required_options():
    """`geoqc api serve --help` показывает --host, --port и --reload."""
    result = CliRunner().invoke(root_app, ["api", "serve", "--help"])
    assert result.exit_code == 0, result.output
    for opt in ("--host", "--port", "--reload"):
        assert opt in result.output


def test_asgi_app_importable():
    """`geoqc.api.main:app` импортируется и является FastAPI-приложением с нужными маршрутами."""
    module = importlib.import_module("geoqc.api.main")
    app = module.app
    paths = {getattr(r, "path", None) for r in app.routes}
    assert app.title == "geoqc API"
    paths_all = set(paths)
    for router in (
        module.meta.router,
        module.discrepancies.router,
        module.tiles.router,
        module.imagery.router,
    ):
        paths_all |= {r.path for r in router.routes}
    assert {
        "/health",
        "/stats",
        "/config/map",
        "/discrepancies",
        "/discrepancies/{disc_id}",
        "/export",
        "/tiles/{z}/{x}/{y}.mvt",
        "/imagery/{z}/{x}/{y}.png",
        "/basemap/esri/{z}/{y}/{x}",
    } <= paths_all
