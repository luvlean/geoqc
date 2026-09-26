"""Review queue endpoints: list / get / patch status / export (docs/CONTRACTS.md §8, §3.8)."""

from __future__ import annotations

import json
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg import sql

from geoqc.api.deps import DISCREPANCY_CLASSES, DISCREPANCY_STATUSES, AppContext, get_ctx
from geoqc.api.schemas import (
    BBox,
    DiscrepancyFilter,
    DiscrepancyProperties,
    ExportProperties,
    Feature,
    FeatureCollection,
    StatusPatch,
    parse_csv_enum,
)

router = APIRouter(tags=["discrepancies"])

GEOJSON_MEDIA_TYPE = "application/geo+json"

# Columns shared by every discrepancy query; ``ref_osm_id`` comes from buildings_ref.
_SELECT_COLUMNS = sql.SQL(
    """
    d.id, d.class, d.confidence, d.iou, d.status, d.ref_id, d.det_id,
    r.osm_id AS ref_osm_id, d.model_version, d.created_at, d.updated_at,
    ST_AsGeoJSON(d.geom, 7)::json AS geometry
    """
)
_FROM = sql.SQL("FROM discrepancies d LEFT JOIN buildings_ref r ON r.id = d.ref_id")


def _query_error(exc: ValueError, param: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=[{"loc": ["query", param], "msg": str(exc), "type": "value_error"}],
    )


def parse_filters(
    bbox: str | None,
    class_: str | None,
    min_conf: float | None,
    status: str | None,
) -> DiscrepancyFilter:
    """Turn raw query parameters into a validated :class:`DiscrepancyFilter` (422 on error)."""
    try:
        parsed_bbox = BBox.parse(bbox) if bbox else None
    except ValueError as exc:
        raise _query_error(exc, "bbox") from exc
    try:
        classes = parse_csv_enum(class_, DISCREPANCY_CLASSES, "class")
    except ValueError as exc:
        raise _query_error(exc, "class") from exc
    try:
        statuses = parse_csv_enum(status, DISCREPANCY_STATUSES, "status")
    except ValueError as exc:
        raise _query_error(exc, "status") from exc
    return DiscrepancyFilter(
        bbox=parsed_bbox, classes=classes, min_conf=min_conf, statuses=statuses
    )


def build_where(flt: DiscrepancyFilter) -> tuple[sql.Composable, list[Any]]:
    """Compose the WHERE clause with bound parameters (never string-format user input)."""
    clauses: list[sql.Composable] = []
    params: list[Any] = []
    if flt.bbox is not None:
        clauses.append(sql.SQL("d.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)"))
        params.extend(flt.bbox.as_tuple())
    if flt.classes:
        clauses.append(sql.SQL("d.class = ANY(%s)"))
        params.append(flt.classes)
    if flt.min_conf is not None:
        clauses.append(sql.SQL("d.confidence >= %s"))
        params.append(flt.min_conf)
    if flt.statuses:
        clauses.append(sql.SQL("d.status = ANY(%s)"))
        params.append(flt.statuses)
    if not clauses:
        return sql.SQL(""), params
    return sql.SQL("WHERE ") + sql.SQL(" AND ").join(clauses), params


def row_to_feature(row: dict[str, Any], *, export: bool = False) -> Feature:
    model = ExportProperties if export else DiscrepancyProperties
    props = model.model_validate(row).model_dump(by_alias=True, mode="json")
    return Feature(id=int(row["id"]), geometry=row["geometry"], properties=props)


_FEATURE_COLUMNS = (
    "score", "area_m2", "area_norm", "secondary_coverage", "has_secondary",
    "max_iou_any", "aoi_edge", "confidence_raw", "confidence",
)  # fmt: skip


def fetch_feature(
    conn: psycopg.Connection, disc_id: int, *, with_scoring: bool = False
) -> Feature | None:
    """One discrepancy; ``with_scoring`` adds ``properties.features`` from discrepancy_features (D-012)."""
    query = sql.SQL("SELECT {cols} {frm} WHERE d.id = %s").format(cols=_SELECT_COLUMNS, frm=_FROM)
    row = conn.execute(query, [disc_id]).fetchone()
    if not row:
        return None
    feature = row_to_feature(row)
    if with_scoring:
        scoring = conn.execute(
            sql.SQL("SELECT {cols} FROM discrepancy_features WHERE discrepancy_id = %s").format(
                cols=sql.SQL(", ").join(sql.Identifier(c) for c in _FEATURE_COLUMNS)
            ),
            [disc_id],
        ).fetchone()
        feature.properties["features"] = dict(scoring) if scoring else None
    return feature


def _geojson_response(fc: FeatureCollection, headers: dict[str, str] | None = None) -> Response:
    return Response(
        content=fc.model_dump_json(exclude_none=True),
        media_type=GEOJSON_MEDIA_TYPE,
        headers=headers,
    )


@router.get("/discrepancies", response_model=FeatureCollection, summary="Review queue (GeoJSON)")
def list_discrepancies(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    bbox: Annotated[str | None, Query(description="minx,miny,maxx,maxy in EPSG:4326")] = None,
    class_: Annotated[str | None, Query(alias="class", description="comma-separated")] = None,
    min_conf: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    status: Annotated[str | None, Query(description="comma-separated")] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    """FeatureCollection sorted by ``confidence DESC``; ``numberMatched`` counts all filtered rows."""
    pag = ctx.config["pagination"]
    if limit is None:
        limit = int(pag["default_limit"])
    if limit > int(pag["max_limit"]):
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["query", "limit"],
                    "msg": f"limit must be <= {pag['max_limit']}",
                    "type": "value_error",
                }
            ],
        )
    flt = parse_filters(bbox, class_, min_conf, status)
    where, params = build_where(flt)
    query = sql.SQL(
        "SELECT {cols}, count(*) OVER () AS total {frm} {where} "
        "ORDER BY d.confidence DESC, d.id ASC LIMIT %s OFFSET %s"
    ).format(cols=_SELECT_COLUMNS, frm=_FROM, where=where)
    with ctx.connection() as conn:
        rows = conn.execute(query, [*params, limit, offset]).fetchall()
    total = int(rows[0]["total"]) if rows else 0
    if not rows and offset > 0:
        # Window count is unavailable beyond the last page; fall back to a plain count.
        count_query = sql.SQL("SELECT count(*) AS n {frm} {where}").format(frm=_FROM, where=where)
        with ctx.connection() as conn:
            total = int(conn.execute(count_query, params).fetchone()["n"])
    fc = FeatureCollection(
        features=[row_to_feature(r) for r in rows],
        numberReturned=len(rows),
        numberMatched=total,
    )
    return _geojson_response(fc)


@router.get("/discrepancies/{disc_id}", response_model=Feature, summary="One discrepancy")
def get_discrepancy(disc_id: int, ctx: Annotated[AppContext, Depends(get_ctx)]) -> Response:
    """Feature with ``properties.features`` (scoring inputs) when sql/002 is applied."""
    with ctx.connection() as conn:
        feature = fetch_feature(conn, disc_id, with_scoring=ctx.has_matching_tables(conn))
    if feature is None:
        raise HTTPException(status_code=404, detail="discrepancy not found")
    return Response(content=feature.model_dump_json(), media_type=GEOJSON_MEDIA_TYPE)


@router.patch("/discrepancies/{disc_id}", response_model=Feature, summary="Set review status")
def patch_discrepancy(
    disc_id: int, body: StatusPatch, ctx: Annotated[AppContext, Depends(get_ctx)]
) -> Response:
    """Update ``status`` and ``updated_at``; returns the updated Feature."""
    with ctx.connection() as conn:
        updated = conn.execute(
            "UPDATE discrepancies SET status = %s, updated_at = now() WHERE id = %s RETURNING id",
            [body.status, disc_id],
        ).fetchone()
        if updated is None:
            raise HTTPException(status_code=404, detail="discrepancy not found")
        feature = fetch_feature(conn, disc_id, with_scoring=ctx.has_matching_tables(conn))
    assert feature is not None
    return Response(content=feature.model_dump_json(), media_type=GEOJSON_MEDIA_TYPE)


@router.get("/export", summary="Export review queue as GeoJSON (contract §3.8)")
def export_discrepancies(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    class_: Annotated[str | None, Query(alias="class")] = None,
    min_conf: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    status: Annotated[str | None, Query()] = None,
    bbox: Annotated[str | None, Query()] = None,
) -> Response:
    """Whole filtered queue (no paging), properties exactly as in §3.8; served as a download."""
    flt = parse_filters(bbox, class_, min_conf, status)
    where, params = build_where(flt)
    query = sql.SQL("SELECT {cols} {frm} {where} ORDER BY d.confidence DESC, d.id ASC").format(
        cols=_SELECT_COLUMNS, frm=_FROM, where=where
    )
    with ctx.connection() as conn:
        rows = conn.execute(query, params).fetchall()
    features = [row_to_feature(r, export=True) for r in rows]
    payload = {
        "type": "FeatureCollection",
        "name": "discrepancies",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [f.model_dump(mode="json") for f in features],
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type=GEOJSON_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="discrepancies.geojson"'},
    )
