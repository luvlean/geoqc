"""CLI `geoqc matching` через typer.testing.CliRunner на тестовой БД (--test-db)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from geoqc.config import load_config
from geoqc.matching import cli as matching_cli
from geoqc.matching.cli import app

from .conftest import METRIC_SRID, sq

pytestmark = pytest.mark.db
runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_real_aoi(monkeypatch):
    # synthetic squares lie outside the real config/aoi.geojson; keep the CLI hermetic
    def _load():
        cfg = load_config()
        cfg["matching"]["aoi_path"] = None
        return cfg

    monkeypatch.setattr(matching_cli, "load_config", _load)


def test_init_db_applies_sql(db_conn):
    """init-db применяет 001 и 002 без ошибок (идемпотентно)."""
    r = runner.invoke(app, ["init-db", "--test-db"])
    assert r.exit_code == 0, r.output
    assert "002_matching.sql" in r.output


def test_run_export_stats_roundtrip(db, db_conn, tmp_path):
    """run → stats → export на тестовой БД; счётчики по классам совпадают."""
    db.ref(sq(0, 0, 10, 10), osm_id=1)
    db.ref(sq(100, 0, 110, 10), osm_id=2)
    db.det(sq(0, 0, 10, 10), model_version="m-cli")
    db.det(sq(200, 0, 210, 10), model_version="m-cli")
    db_conn.commit()  # the CLI opens its own connection

    r = runner.invoke(
        app,
        [
            "run",
            "--test-db",
            "--model-version",
            "m-cli",
            "--metric-srid",
            str(METRIC_SRID),
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    res = json.loads(r.output)
    assert res["n_matches"] == 1
    assert res["counts_by_class"] == {
        "missing_in_map": 1,
        "missing_on_image": 1,
        "geometry_mismatch": 0,
    }

    s = runner.invoke(app, ["stats", "--test-db", "--json"])
    assert s.exit_code == 0, s.output
    st = json.loads(s.output)
    assert st["model_version"] == "m-cli"
    assert st["matches"] == 1
    assert st["discrepancies"]["missing_in_map"]["total"] == 1
    assert st["discrepancies"]["missing_on_image"]["by_status"] == {"new": 1}

    out = tmp_path / "d.geojson"
    e = runner.invoke(app, ["export", "--test-db", "--out", str(out), "--model-version", "m-cli"])
    assert e.exit_code == 0, e.output
    assert len(json.loads(out.read_text())["features"]) == 2

    txt = runner.invoke(app, ["stats", "--test-db"])
    assert txt.exit_code == 0 and "missing_in_map" in txt.output


def test_run_unknown_model_version_exits_nonzero(db, db_conn):
    """Ошибка резолва model_version → код возврата 1 и сообщение."""
    db.det(sq(0, 0, 10, 10), model_version="m-a")
    db_conn.commit()
    r = runner.invoke(
        app, ["run", "--test-db", "--model-version", "nope", "--metric-srid", str(METRIC_SRID)]
    )
    assert r.exit_code == 1
    assert "model_version" in (r.output + str(r.exception))
