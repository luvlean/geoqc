"""``geoqc api`` commands: ``serve`` (uvicorn) and ``bench`` (bbox-query latency on synthetic rows)."""

from __future__ import annotations

import json
import statistics
import time
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, help="FastAPI service and web map")


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option(help="bind address (default: config/api.yaml)")
    ] = None,
    port: Annotated[int | None, typer.Option(help="port (default: config/api.yaml)")] = None,
    reload: Annotated[bool, typer.Option("--reload", help="auto-reload on code changes")] = False,
    log_level: Annotated[str, typer.Option(help="uvicorn log level")] = "info",
) -> None:
    """Run the API + web map with uvicorn (ASGI app: geoqc.api.main:app)."""
    import uvicorn

    from geoqc.api.deps import load_api_config

    server = load_api_config()["server"]
    uvicorn.run(
        "geoqc.api.main:app",
        host=host or str(server["host"]),
        port=port if port is not None else int(server["port"]),
        reload=reload,
        log_level=log_level,
    )


@app.command()
def bench(
    n: Annotated[int, typer.Option(help="synthetic discrepancies to insert")] = 5000,
    repeats: Annotated[int, typer.Option(help="timed requests per endpoint")] = 10,
    keep: Annotated[bool, typer.Option("--keep", help="do not truncate tables afterwards")] = False,
) -> None:
    """Insert N synthetic rows into the TEST database and time GET /discrepancies over the AOI bbox.

    Prints JSON with EXPLAIN ANALYZE of the bbox query and request latencies (ms).
    Uses the test DB only (TEST_DATABASE_URL); tables are truncated at the end unless --keep.
    """
    from fastapi.testclient import TestClient

    from geoqc.api.main import create_app
    from geoqc.db import apply_schema, connect, get_dsn

    dsn = get_dsn(test=True)
    bbox = (30.30, 59.93, 30.36, 59.96)  # ~3.3 x 3.3 km => ~11 km2 synthetic AOI
    with connect(dsn) as conn:
        apply_schema(conn)
        conn.execute(
            "TRUNCATE discrepancies, matches, buildings_det, buildings_ref RESTART IDENTITY CASCADE"
        )
        conn.execute(
            """
            INSERT INTO discrepancies (class, iou, confidence, geom, status, model_version)
            SELECT (ARRAY['missing_in_map','missing_on_image','geometry_mismatch'])[1 + (g %% 3)],
                   CASE WHEN g %% 3 = 2 THEN 0.1 + 0.4 * random() ELSE 0 END,
                   random(),
                   ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(
                       %(minx)s + (%(maxx)s - %(minx)s) * random(),
                       %(miny)s + (%(maxy)s - %(miny)s) * random()), 4326), 0.00012, 1)),
                   (ARRAY['new','new','new','confirmed','rejected'])[1 + (g %% 5)],
                   'bench-0000000'
            FROM generate_series(1, %(n)s) AS g
            """,
            {"minx": bbox[0], "miny": bbox[1], "maxx": bbox[2], "maxy": bbox[3], "n": n},
        )
        conn.execute("ANALYZE discrepancies")
        plan = conn.execute(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
            SELECT d.id, d.class, d.confidence, d.iou, d.status, d.ref_id, d.det_id,
                   r.osm_id, d.model_version, d.created_at, d.updated_at,
                   ST_AsGeoJSON(d.geom, 7)::json, count(*) OVER ()
            FROM discrepancies d LEFT JOIN buildings_ref r ON r.id = d.ref_id
            WHERE d.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            ORDER BY d.confidence DESC, d.id LIMIT 500 OFFSET 0
            """,
            list(bbox),
        ).fetchall()
        small = (
            bbox[0],
            bbox[1],
            bbox[0] + (bbox[2] - bbox[0]) / 10,
            bbox[1] + (bbox[3] - bbox[1]) / 10,
        )
        plan_small = conn.execute(
            "EXPLAIN (ANALYZE, FORMAT TEXT) SELECT d.id FROM discrepancies d"
            " WHERE d.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
            " ORDER BY d.confidence DESC LIMIT 500",
            list(small),
        ).fetchall()
    bbox_str = ",".join(str(v) for v in bbox)
    timings: dict[str, list[float]] = {}
    requests_ = {
        "list_bbox_limit500": f"/discrepancies?bbox={bbox_str}",
        "list_bbox_limit5000": f"/discrepancies?bbox={bbox_str}&limit=5000",
        "list_bbox_class_minconf": f"/discrepancies?bbox={bbox_str}&class=missing_in_map&min_conf=0.5",
        "export_all": "/export",
        "tile_z14": "/tiles/14/9572/4762.mvt",
        "stats": "/stats",
    }
    with TestClient(create_app(dsn=dsn, serve_static=False)) as client:
        for name, url in requests_.items():
            samples: list[float] = []
            for _ in range(repeats):
                t0 = time.perf_counter()
                resp = client.get(url)
                samples.append((time.perf_counter() - t0) * 1000.0)
                if resp.status_code != 200:
                    raise typer.Exit(code=f"{url} -> HTTP {resp.status_code}: {resp.text[:200]}")
            timings[name] = samples
    result = {
        "n_rows": n,
        "bbox": bbox,
        "explain": [row["QUERY PLAN"] for row in plan],
        "explain_small_bbox": [row["QUERY PLAN"] for row in plan_small],
        "latency_ms": {
            k: {"median": round(statistics.median(v), 1), "max": round(max(v), 1)}
            for k, v in timings.items()
        },
    }
    if not keep:
        with connect(dsn) as conn:
            conn.execute(
                "TRUNCATE discrepancies, matches, buildings_det, buildings_ref RESTART IDENTITY CASCADE"
            )
    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))
