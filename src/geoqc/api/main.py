"""ASGI entry point: ``uvicorn geoqc.api.main:app`` or ``geoqc api serve``.

:func:`create_app` builds an isolated application (used by tests with the test DSN);
the module-level ``app`` uses ``.env`` settings and ``config/api.yaml``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse, Response

from geoqc import __version__
from geoqc.api.deps import REVIEW_ENV, AppContext
from geoqc.api.routes import discrepancies, imagery, meta, tiles, verification

log = logging.getLogger("geoqc.api")


_LOCAL_ASSET = re.compile(r'(src|href)="/([\w.-]+\.(?:js|css))"')


def versioned_index(web_dir: Path) -> str:
    """index.html with ``?v=<newest mtime in web/>`` on local js/css.

    Safari reuses a cached stylesheet even with ``no-cache``; a new URL after every edit cannot be
    stale. app.js passes the same ``v`` on to the files it loads itself (review.js, review.css).
    """
    version = max(int(f.stat().st_mtime) for f in web_dir.iterdir() if f.is_file())
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    return _LOCAL_ASSET.sub(rf'\1="/\2?v={version}"', html)


class RevalidatingStaticFiles(StaticFiles):
    """web/ files with ``Cache-Control: no-cache``: an edited app.js/style.css is never served stale."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    *,
    dsn: str | None = None,
    config: dict[str, Any] | None = None,
    arcgis_api_key: str | None = None,
    paths: dict[str, str | Path] | None = None,
    serve_static: bool = True,
    review: bool | None = None,
) -> FastAPI:
    """Build the FastAPI app. Keyword overrides exist for tests (test DSN, tmp paths, no key).

    ``review`` turns on the tester verification tools; ``None`` takes ``GEOQC_API_REVIEW=1``
    (set by ``geoqc api serve --review``) or ``review.enabled`` from ``config/api.yaml``.
    """
    ctx = AppContext.build(dsn=dsn, config=config, arcgis_api_key=arcgis_api_key, paths=paths)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ctx.open_pool()
        log.info("geoqc api %s started; db=%s", __version__, ctx.db_ok(timeout=1.0))
        try:
            yield
        finally:
            ctx.close_pool()

    if review is None:
        review = os.environ.get(REVIEW_ENV) == "1" or bool(ctx.config["review"]["enabled"])
    ctx.review_enabled = review

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
    if ctx.review_enabled:
        app.include_router(verification.router)

    web_dir = ctx.paths["web"]
    if serve_static and web_dir.is_dir():

        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        def index() -> HTMLResponse:
            return HTMLResponse(versioned_index(web_dir), headers={"Cache-Control": "no-cache"})

        # Mounted last so API routes take precedence; html=True serves index.html at "/".
        app.mount("/", RevalidatingStaticFiles(directory=str(web_dir), html=True), name="web")
    return app


app = create_app()
