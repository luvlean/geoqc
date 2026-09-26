"""Mapbox Vector Tiles: ``GET /tiles/{z}/{x}/{y}.mvt`` with layers discrepancies / buildings_ref / buildings_det."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg import sql

from geoqc.api.deps import AppContext, get_ctx

router = APIRouter(tags=["tiles"])

MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"

# Per-layer attribute lists. ``fid`` becomes the MVT feature id (consumed by ST_AsMVT), while
# ``id`` and ``layer`` stay in the properties because Leaflet.VectorGrid exposes only properties.
_LAYER_ATTRS: dict[str, sql.SQL] = {
    "discrepancies": sql.SQL(
        "t.id AS fid, t.id, 'discrepancies'::text AS layer, t.class, t.confidence, t.iou,"
        " t.status, t.ref_id, t.det_id, t.model_version"
    ),
    "buildings_ref": sql.SQL(
        "t.id AS fid, t.id, 'buildings_ref'::text AS layer, t.osm_id, t.source"
    ),
    "buildings_det": sql.SQL(
        "t.id AS fid, t.id, 'buildings_det'::text AS layer, t.score, t.model_version, t.tile_id"
    ),
}


# Extra predicates applied when sql/002_matching.sql is present (DECISIONS.md D-012):
# NMS-suppressed detections stay in buildings_det but must not be shown.
_LAYER_EXTRA_WHERE: dict[str, sql.SQL] = {
    "buildings_det": sql.SQL(
        " AND NOT EXISTS (SELECT 1 FROM det_suppressed s"
        " WHERE s.det_id = t.id AND s.model_version = t.model_version)"
    ),
}


def validate_tile(z: int, x: int, y: int, max_zoom: int) -> None:
    if z < 0 or z > max_zoom:
        raise HTTPException(status_code=422, detail=f"z must be within [0, {max_zoom}]")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise HTTPException(status_code=422, detail=f"x and y must be within [0, {n - 1}] at z={z}")


def build_mvt_query(
    layers: list[str],
    tables: dict[str, str],
    extent: int,
    buffer: int,
    *,
    matching_tables: bool = False,
) -> sql.SQL:
    """One statement returning the concatenated MVT of all requested layers (empty -> '')."""
    parts: list[sql.Composable] = []
    for name in layers:
        extra = _LAYER_EXTRA_WHERE.get(name, sql.SQL("")) if matching_tables else sql.SQL("")
        parts.append(
            sql.SQL(
                "COALESCE((SELECT ST_AsMVT(q, {layer}, {extent}, 'geom', 'fid') FROM ("
                " SELECT ST_AsMVTGeom(ST_Transform(t.geom, 3857), b.tile, {extent}, {buffer}, true)"
                " AS geom, {attrs} FROM {table} t, bounds b WHERE t.geom && b.query_4326{extra}"
                ") q WHERE q.geom IS NOT NULL), ''::bytea)"
            ).format(
                layer=sql.Literal(name),
                extent=sql.Literal(extent),
                buffer=sql.Literal(buffer),
                attrs=_LAYER_ATTRS[name],
                table=sql.Identifier(tables[name]),
                extra=extra,
            )
        )
    body = sql.SQL(" || ").join(parts) if parts else sql.SQL("''::bytea")
    return sql.SQL(
        "WITH bounds AS ("
        " SELECT ST_TileEnvelope(%(z)s, %(x)s, %(y)s) AS tile,"
        " ST_Transform(ST_TileEnvelope(%(z)s, %(x)s, %(y)s, margin => %(margin)s), 4326)"
        " AS query_4326"
        ") SELECT {body} AS mvt FROM bounds"
    ).format(body=body)


@router.get("/tiles/{z}/{x}/{y}.mvt", summary="Vector tile (MVT)")
def get_tile(
    z: int,
    x: int,
    y: int,
    ctx: Annotated[AppContext, Depends(get_ctx)],
    layers: Annotated[
        str | None, Query(description="comma-separated subset of the configured layers")
    ] = None,
) -> Response:
    mvt_cfg = ctx.config["mvt"]
    validate_tile(z, x, y, int(mvt_cfg["max_zoom"]))
    configured: dict[str, dict] = mvt_cfg["layers"]
    if layers:
        requested = [n.strip() for n in layers.split(",") if n.strip()]
        unknown = [n for n in requested if n not in configured]
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown layer(s): {', '.join(unknown)}")
    else:
        requested = list(configured)
    active = [n for n in requested if z >= int(configured[n].get("min_zoom", 0))]
    headers = {"Cache-Control": ctx.cache_header("tiles")}
    if not active:
        return Response(content=b"", media_type=MVT_MEDIA_TYPE, headers=headers)
    extent, buffer = int(mvt_cfg["extent"]), int(mvt_cfg["buffer"])
    params = {"z": z, "x": x, "y": y, "margin": buffer / extent}
    with ctx.connection() as conn:
        query = build_mvt_query(
            active,
            {n: configured[n]["table"] for n in active},
            extent,
            buffer,
            matching_tables=ctx.has_matching_tables(conn),
        )
        row = conn.execute(query, params).fetchone()
    data = bytes(row["mvt"]) if row and row["mvt"] is not None else b""
    return Response(content=data, media_type=MVT_MEDIA_TYPE, headers=headers)
