"""Юнит-тесты pydantic-схем и парсеров параметров (без БД)."""

from __future__ import annotations

import pytest

from geoqc.api.deps import DISCREPANCY_CLASSES, _deep_merge, load_api_config
from geoqc.api.schemas import BBox, ExportProperties, Feature, FeatureCollection, parse_csv_enum


def test_bbox_parse_ok():
    """Корректная строка bbox разбирается в четыре числа в порядке minx, miny, maxx, maxy."""
    bb = BBox.parse(" 30.3, 59.93 ,30.36,59.96")
    assert bb.as_tuple() == (30.3, 59.93, 30.36, 59.96)


@pytest.mark.parametrize(
    "text",
    ["1,2,3", "a,b,c,d", "30.4,59.9,30.3,60.0", "30.3,60.0,30.4,59.9", "-181,0,10,10", "0,-91,1,1"],
)
def test_bbox_parse_rejects_bad_input(text):
    """Неполный, нечисловой, вырожденный или выходящий за пределы WGS84 bbox отклоняется."""
    with pytest.raises(ValueError):
        BBox.parse(text)


def test_parse_csv_enum_dedup_and_validation():
    """Список через запятую валидируется по допустимым значениям и очищается от дубликатов."""
    assert parse_csv_enum(
        "missing_in_map, missing_in_map,geometry_mismatch", DISCREPANCY_CLASSES, "class"
    ) == [
        "missing_in_map",
        "geometry_mismatch",
    ]
    assert parse_csv_enum("", DISCREPANCY_CLASSES, "class") is None
    with pytest.raises(ValueError, match="unknown class"):
        parse_csv_enum("foo", DISCREPANCY_CLASSES, "class")


def test_export_properties_alias_class():
    """Свойство ``class`` сериализуется под именем контракта §3.8, а не как ``class_``."""
    props = ExportProperties.model_validate(
        {
            "id": 1,
            "class": "missing_in_map",
            "confidence": 0.5,
            "iou": None,
            "status": "new",
            "ref_osm_id": None,
            "model_version": "m-1",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ).model_dump(by_alias=True, mode="json")
    assert set(props) == {
        "id",
        "class",
        "confidence",
        "iou",
        "status",
        "ref_osm_id",
        "model_version",
        "created_at",
    }
    assert props["class"] == "missing_in_map"


def test_feature_collection_shape():
    """FeatureCollection содержит type, features и счётчики numberReturned/numberMatched."""
    fc = FeatureCollection(
        features=[Feature(id=1, geometry={"type": "Point", "coordinates": [0, 0]}, properties={})],
        numberReturned=1,
        numberMatched=10,
    )
    data = fc.model_dump()
    assert data["type"] == "FeatureCollection"
    assert data["features"][0]["type"] == "Feature"
    assert (data["numberReturned"], data["numberMatched"]) == (1, 10)


def test_api_config_defaults_and_merge(tmp_path):
    """Отсутствующий api.yaml даёт значения по умолчанию, частичный файл сливается поверх них."""
    cfg = load_api_config(tmp_path / "missing.yaml")
    assert cfg["pagination"]["default_limit"] == 500
    partial = tmp_path / "api.yaml"
    partial.write_text("pagination: {default_limit: 7}\n", encoding="utf-8")
    merged = load_api_config(partial)
    assert merged["pagination"] == {"default_limit": 7, "max_limit": 5000}
    assert _deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}
