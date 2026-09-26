"""Тесты загрузки в buildings_ref (PostGIS, маркер db)."""

from __future__ import annotations

import pytest

from geoqc.data.loadref import count_by_source, load_features

SQ = {
    "type": "Polygon",
    "coordinates": [[[30.0, 59.0], [30.001, 59.0], [30.001, 59.001], [30.0, 59.001], [30.0, 59.0]]],
}
BOWTIE = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}


def _feat(osm_id: int, geom=SQ, tags=None):
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "osm_id": osm_id,
            "tags": tags or {"building": "yes"},
            "snapshot_date": "2026-06-01",
        },
    }


@pytest.mark.db
def test_load_ref_is_idempotent_and_deduplicates(db_conn) -> None:
    """Повторная загрузка того же источника не удваивает строки; дубликаты osm_id схлопываются; другой источник не затирается."""
    feats = [_feat(1), _feat(2), _feat(2), _feat(3, geom=BOWTIE)]
    n1 = load_features(db_conn, feats, source="osm")
    assert n1 == 3  # 1, 2 (dedup), 3 (bow-tie made valid)
    n2 = load_features(db_conn, feats, source="osm")
    assert n2 == 3
    m = load_features(db_conn, [_feat(10), _feat(11)], source="microsoft")
    assert m == 2
    assert count_by_source(db_conn) == {"microsoft": 2, "osm": 3}
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT osm_id, ST_GeometryType(geom) AS t, ST_IsValid(geom) AS v, tags FROM buildings_ref WHERE source='osm' ORDER BY osm_id"
        )
        rows = cur.fetchall()
    assert [r["osm_id"] for r in rows] == [1, 2, 3]
    assert all(r["t"] == "ST_MultiPolygon" and r["v"] for r in rows)
    assert rows[0]["tags"] == {"building": "yes"}


@pytest.mark.db
def test_load_ref_rejects_unknown_source(db_conn) -> None:
    """Источник вне CHECK-ограничения схемы отклоняется до обращения к БД."""
    with pytest.raises(ValueError):
        load_features(db_conn, [_feat(1)], source="yandex")
