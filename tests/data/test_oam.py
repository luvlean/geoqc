"""Тесты разбора ответа OpenAerialMap и ранжирования кандидатов (фикстура oam_response.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from geoqc.data.aoi import bbox_area_km2, evaluate_item, footprint_area_km2, in_bbox, score

CRIT = {
    "gsd_max_m": 0.5,
    "min_image_area_km2": 5.0,
    "max_image_area_km2": 60.0,
    "min_year": 2015,
    "licenses_ok": ["CC-BY 4.0", "CC0", "CC BY-SA 4.0"],
    "exclude_title_regex": r"(?i)\b(DEM|DSM|DTM)\b",
    "russia_bbox": [27.3, 43.5, 180.0, 82.0],
}


@pytest.fixture
def oam(fixtures_dir: Path) -> list[dict]:
    with (fixtures_dir / "oam_response.json").open(encoding="utf-8") as fh:
        return json.load(fh)["results"]


def test_fixture_parses_and_areas_are_positive(oam: list[dict]) -> None:
    """Каждая запись содержит bbox, gsd и лицензию; площадь по footprint не больше площади bbox."""
    assert len(oam) == 6
    for item in oam:
        assert item["bbox"] and len(item["bbox"]) == 4
        fp = footprint_area_km2(item)
        assert 0 <= fp <= bbox_area_km2(item["bbox"]) + 1e-9


def test_evaluate_item_accepts_spb_orthophotos_and_rejects_others(oam: list[dict]) -> None:
    """Проходят только снимки СПб 2020 (RGB, CC-BY, >= 5 км²); DEM, CC BY-NC, 2009 г. и микроснимки отсеиваются с причиной."""
    ev = {c["oam_id"]: c for c in (evaluate_item(x, CRIT) for x in oam)}
    ok = [c for c in ev.values() if c["rejected"] is None]
    assert {c["title"][:5] for c in ok} == {"СПб, "}
    assert len(ok) == 2
    assert all(c["in_russia_bbox"] for c in ok)
    dem = ev["5f3f9933a646430007b2ac93"]
    assert "DEM" in dem["rejected"]
    nc = ev["661574d7e89cf30001e0c3c2"]
    assert "license=CC BY-NC 4.0" in nc["rejected"] and "area<" in nc["rejected"]
    old = ev["59e62b8f3d6412ef72209fdf"]
    assert "too old" in old["rejected"]
    dsm = ev["6634a08f6049ef00013b82b5"]
    assert "DSM" in dsm["rejected"] or "area<" in dsm["rejected"]


def test_score_prefers_russia_recent_fine_gsd_and_osm_density() -> None:
    """Скоринг: снимок в РФ, свежий, с мелким GSD и плотной смешанной застройкой получает больше очков."""
    base = {"in_russia_bbox": False, "acquired": "2016-01-01", "gsd_m": 0.5, "area_km2": 3.0}
    ru = dict(base, in_russia_bbox=True)
    assert score(ru) > score(base)
    recent = dict(base, acquired="2024-01-01")
    assert score(recent) > score(base)
    fine = dict(base, gsd_m=0.1)
    assert score(fine) > score(base)
    dense = dict(
        base,
        osm_buildings_per_5km2=1000.0,
        osm_mix={"apartments": 50, "house": 50, "industrial": 20},
    )
    assert score(dense) > score(base) + 30
    bad_header = dict(base, header_ok=False)
    assert score(bad_header) < -500


def test_in_bbox_uses_centre() -> None:
    """Принадлежность bbox определяется по центру снимка."""
    assert in_bbox({"bbox": [30.0, 59.0, 31.0, 60.0]}, (27.3, 43.5, 180.0, 82.0))
    assert not in_bbox({"bbox": [20.0, 50.0, 21.0, 51.0]}, (27.3, 43.5, 180.0, 82.0))
    assert not in_bbox({}, (0, 0, 1, 1))
