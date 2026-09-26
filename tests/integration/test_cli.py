"""Корневой CLI и все саб-приложения отвечают на --help."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from geoqc.cli import app

runner = CliRunner()
SUBAPPS = ["data", "annotation", "cv", "matching", "eval", "api"]


def test_root_help_lists_all_subapps():
    """geoqc --help перечисляет все шесть модулей CONTRACTS §6."""
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0, r.output
    for name in SUBAPPS:
        assert name in r.output


@pytest.mark.parametrize("name", SUBAPPS)
def test_subapp_help(name):
    """geoqc <module> --help завершается с кодом 0."""
    r = runner.invoke(app, [name, "--help"])
    assert r.exit_code == 0, r.output
