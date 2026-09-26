"""Root CLI: ``geoqc <module> <command>``. Each module registers its own Typer app.

Module owners add exactly one line here for their sub-app (see docs/CONTRACTS.md §6).
Missing modules are skipped so the CLI stays importable while work is in progress.
"""

from __future__ import annotations

import importlib

import typer

app = typer.Typer(no_args_is_help=True, help="geoqc: geospatial building data quality control")

_SUBAPPS = {
    "data": "geoqc.data.cli",
    "annotation": "geoqc.annotation.cli",
    "cv": "geoqc.cv.cli",
    "matching": "geoqc.matching.cli",
    "eval": "geoqc.eval.cli",
    "api": "geoqc.api.cli",
}

for _name, _module in _SUBAPPS.items():
    try:
        _mod = importlib.import_module(_module)
    except ModuleNotFoundError as exc:  # module not implemented yet
        if exc.name and exc.name.startswith(_module.rsplit(".", 1)[0]):
            continue
        raise
    app.add_typer(_mod.app, name=_name)


@app.callback()
def main() -> None:
    """geoqc command group; use `geoqc <module> --help` for module commands."""


@app.command()
def version() -> None:
    """Print package version."""
    from geoqc import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
