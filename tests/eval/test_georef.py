"""Чтение reports/georef.json в блок georeference."""

from __future__ import annotations

from geoqc.eval.common import write_json
from geoqc.eval.georef import read_georef


def test_read_georef_variants(tmp_path):
    """Разные написания ключей читаются, значение «после коррекции» приоритетнее."""
    p = tmp_path / "georef.json"
    write_json(p, {"median_offset_m": 3.1, "median_offset_after_m": 0.9, "n_control_points": 14})
    res = read_georef(p)
    assert res["median_offset_m"] == 0.9 and res["n_control_points"] == 14 and res["note"] is None
    write_json(p, {"georef": {"median_offset_m": 1.5, "n_control_points": 10}})
    assert read_georef(p)["median_offset_m"] == 1.5


def test_read_georef_missing_and_manifest_fallback(tmp_path):
    """Нет файла -> None с note; при наличии manifest.json берётся его блок georef."""
    res = read_georef(tmp_path / "georef.json")
    assert res["median_offset_m"] is None and "not found" in res["note"]
    manifest = tmp_path / "manifest.json"
    write_json(
        manifest,
        {
            "georef": {
                "n_control_points": 11,
                "median_offset_m": 1.9,
                "shift_applied_m": [0.5, -0.2],
            }
        },
    )
    res = read_georef(tmp_path / "georef.json", manifest)
    assert (
        res["median_offset_m"] == 1.9
        and res["n_control_points"] == 11
        and "manifest" in res["source"]
    )
    write_json(tmp_path / "georef.json", {"something_else": 1})
    res = read_georef(tmp_path / "georef.json")
    assert res["median_offset_m"] is None and "no median offset key" in res["note"]
