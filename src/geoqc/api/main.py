"""ASGI entry point: ``uvicorn geoqc.api.main:app`` or ``geoqc api serve``.

:func:`create_app` builds an isolated application (used by tests with the test DSN);
the module-level ``app`` uses ``.env`` settings and ``config/api.yaml``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from geoqc import __version__
from geoqc.api.deps import AppContext
from geoqc.api.routes import discrepancies, imagery, meta, tiles

log = logging.getLogger("geoqc.api")


def create_app(
    *,
    dsn: str | None = None,
    config: dict[str, Any] | None = None,
    arcgis_api_key: str | None = None,
    paths: dict[str, str | Path] | None = None,
    serve_static: bool = True,
) -> FastAPI:
    """Build the FastAPI app. Keyword overrides exist for tests (test DSN, tmp paths, no key)."""
    ctx = AppContext.build(dsn=dsn, config=config, arcgis_api_key=arcgis_api_key, paths=paths)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ctx.open_pool()
        log.info("geoqc api %s started; db=%s", __version__, ctx.db_ok(timeout=1.0))
        try:
            yield
        finally:
            ctx.close_pool()

    app = FastAPI(
        title="geoqc API",
        version=__version__,
        description="Review queue of OSM-vs-imagery building discrepancies (docs/CONTRACTS.md §8).",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ctx.config["server"].get("cors_origins", ["*"])),
        allow_methods=["GET", "PATCH", "OPTIONS"],
        allow_headers=["*"],
    )
    app.include_router(meta.router)
    app.include_router(discrepancies.router)
    app.include_router(tiles.router)
    app.include_router(imagery.router)

    web_dir = ctx.paths["web"]
    if serve_static and web_dir.is_dir():
        # Mounted last so API routes take precedence; html=True serves index.html at "/".
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app


app = create_app()
