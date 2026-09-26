"""Тесты полигонизации ответа Overpass (way и relation-multipolygon с дыркой)."""

from __future__ import annotations

from shapely.geometry import shape

from geoqc.data.osm import buffer_bbox_m, overpass_bbox, polygonize_elements, relation_polygon


def _geom(coords):
    return [{"lon": x, "lat": y} for x, y in coords]


SQUARE = [(30.0, 59.0), (30.001, 59.0), (30.001, 59.001), (30.0, 59.001), (30.0, 59.0)]
OUTER_A = [(30.01, 59.01), (30.02, 59.01), (30.02, 59.02)]
OUTER_B = [(30.02, 59.02), (30.01, 59.02), (30.01, 59.01)]
INNER = [(30.013, 59.013), (30.017, 59.013), (30.017, 59.017), (30.013, 59.017), (30.013, 59.013)]


def test_way_is_polygonized_with_contract_properties() -> None:
    """Замкнутый way становится Polygon с osm_id, tags и snapshot_date."""
    els = [{"type": "way", "id": 42, "tags": {"building": "yes"}, "geometry": _geom(SQUARE)}]
    feats = polygonize_elements(els, snapshot_date="2026-06-01")
    assert len(feats) == 1
    f = feats[0]
    assert f["properties"] == {
        "osm_id": 42,
        "osm_type": "way",
        "tags": {"building": "yes"},
        "snapshot_date": "2026-06-01",
    }
    g = shape(f["geometry"])
    assert g.geom_type == "Polygon" and g.is_valid
    assert g.area > 0


def test_relation_with_hole_from_split_outer_ways() -> None:
    """Multipolygon-relation: внешнее кольцо из двух незамкнутых way и внутреннее кольцо дают полигон с дыркой."""
    rel = {
        "type": "relation",
        "id": 7,
        "tags": {"type": "multipolygon", "building": "apartments"},
        "members": [
            {"type": "way", "ref": 1, "role": "outer", "geometry": _geom(OUTER_A)},
            {"type": "way", "ref": 2, "role": "outer", "geometry": _geom(OUTER_B)},
            {"type": "way", "ref": 3, "role": "inner", "geometry": _geom(INNER)},
        ],
    }
    g = relation_polygon(rel)
    assert g is not None and g.is_valid
    assert g.geom_type == "Polygon"
    assert len(g.interiors) == 1
    outer_area = shape({"type": "Polygon", "coordinates": [OUTER_A + OUTER_B[1:]]}).area
    inner_area = shape({"type": "Polygon", "coordinates": [INNER]}).area
    assert (
        g.area == round(outer_area - inner_area, 12)
        or abs(g.area - (outer_area - inner_area)) < 1e-12
    )
    feats = polygonize_elements([rel], snapshot_date="2026-06-01")
    assert feats[0]["properties"]["osm_id"] == -7  # relations get a negative id
    assert feats[0]["properties"]["osm_type"] == "relation"


def test_non_buildings_open_ways_and_duplicates_are_skipped() -> None:
    """building=no, незамкнутые way с < 4 точками и повторные id отбрасываются."""
    els = [
        {"type": "way", "id": 1, "tags": {"building": "no"}, "geometry": _geom(SQUARE)},
        {"type": "way", "id": 2, "tags": {"building": "yes"}, "geometry": _geom(SQUARE[:2])},
        {"type": "way", "id": 3, "tags": {"building": "yes"}, "geometry": _geom(SQUARE)},
        {"type": "way", "id": 3, "tags": {"building": "yes"}, "geometry": _geom(SQUARE)},
        {"type": "node", "id": 4, "tags": {"building": "yes"}, "lat": 59.0, "lon": 30.0},
    ]
    feats = polygonize_elements(els, snapshot_date="2026-06-01")
    assert [f["properties"]["osm_id"] for f in feats] == [3]


def test_min_area_filter_drops_tiny_footprints() -> None:
    """Порог min_area_m2 отсекает полигоны меньше заданной площади."""
    tiny = [(30.0, 59.0), (30.00001, 59.0), (30.00001, 59.00001), (30.0, 59.00001), (30.0, 59.0)]
    els = [{"type": "way", "id": 5, "tags": {"building": "shed"}, "geometry": _geom(tiny)}]
    assert polygonize_elements(els, snapshot_date="x", min_area_m2=4.0) == []
    assert len(polygonize_elements(els, snapshot_date="x", min_area_m2=0.0)) == 1


def test_bbox_helpers() -> None:
    """Overpass bbox записывается как south,west,north,east; буфер в метрах расширяет bbox."""
    assert overpass_bbox((30.0, 59.0, 31.0, 60.0)) == "59.0000000,30.0000000,60.0000000,31.0000000"
    b = buffer_bbox_m((30.0, 59.0, 31.0, 60.0), 111.32)
    assert b[1] < 59.0 < 60.0 < b[3] and b[0] < 30.0 < 31.0 < b[2]
    assert b[3] - 60.0 == b[1] * -1 + 59.0
