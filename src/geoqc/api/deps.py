"""Application context: config, connection pool, paths. Shared by all routers.

The context is created once by :func:`geoqc.api.main.create_app` and stored on
``app.state.ctx``; routers receive it through the :func:`get_ctx` dependency.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import yaml
from fastapi import HTTPException, Request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from geoqc.config import CONFIG_DIR, get_settings, resolve_path

log = logging.getLogger("geoqc.api")

DISCREPANCY_CLASSES: tuple[str, ...] = ("missing_in_map", "missing_on_image", "geometry_mismatch")
DISCREPANCY_STATUSES: tuple[str, ...] = ("new", "confirmed", "rejected")

# Fallback used when config/api.yaml is missing or a key is absent.
DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8000, "cors_origins": ["*"]},
    "pagination": {"default_limit": 500, "max_limit": 5000},
    "pool": {"min_size": 1, "max_size": 8, "timeout_s": 5},
    "cache_seconds": {"tiles": 60, "imagery": 86400, "basemap": 3600},
    "mvt": {
        "extent": 4096,
        "buffer": 64,
        "max_zoom": 22,
        "layers": {
            "discrepancies": {"table": "discrepancies", "min_zoom": 8},
            "buildings_ref": {"table": "buildings_ref", "min_zoom": 13},
            "buildings_det": {"table": "buildings_det", "min_zoom": 13},
        },
    },
    "imagery": {"tile_size": 256, "resampling": "bilinear"},
    "paths": {
        "aoi": "config/aoi.geojson",
        "manifest": "data/manifest.json",
        "cog": "data/interim/aoi_cog.tif",
        "web": "web",
    },
    "basemaps": {
        "osm": {
            "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "attribution": (
                '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                " contributors, ODbL"
            ),
            "max_zoom": 19,
        },
        "esri": {
            "upstream": (
                "https://ibasemaps-api.arcgis.com/arcgis/rest/services/World_Imagery/"
                "MapServer/tile/{z}/{y}/{x}"
            ),
            "attribution": "Esri, Maxar, Earthstar Geographics, and the GIS User Community",
            "max_zoom": 19,
            "timeout_s": 10,
        },
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_api_config(path: Path | None = None) -> dict[str, Any]:
    """Read ``config/api.yaml`` merged over :data:`DEFAULT_CONFIG`.

    Read directly (not via ``load_config``) so that a malformed yaml of another module
    cannot break the API; a missing file yields the defaults.
    """
    cfg_path = path or (CONFIG_DIR / "api.yaml")
    data: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{cfg_path} must contain a mapping at top level")
        data = loaded
    return _deep_merge(DEFAULT_CONFIG, data)


@dataclass
class AppContext:
    """Everything a request handler needs besides its own parameters."""

    config: dict[str, Any]
    dsn: str
    arcgis_api_key: str = ""
    paths: dict[str, Path] = field(default_factory=dict)
    pool: ConnectionPool | None = None
    _matching_tables: bool | None = field(default=None, repr=False)

    @classmethod
    def build(
        cls,
        *,
        dsn: str | None = None,
        config: dict[str, Any] | None = None,
        arcgis_api_key: str | None = None,
        paths: dict[str, str | Path] | None = None,
    ) -> AppContext:
        settings = get_settings()
        cfg = config if config is not None else load_api_config()
        resolved = {k: resolve_path(v) for k, v in cfg["paths"].items()}
        for k, v in (paths or {}).items():
            resolved[k] = resolve_path(v)
        return cls(
            config=cfg,
            dsn=dsn or settings.database_url,
            arcgis_api_key=(
                settings.arcgis_api_key if arcgis_api_key is None else arcgis_api_key
            ).strip(),
            paths=resolved,
        )

    # -- pool lifecycle ---------------------------------------------------------------------
    def open_pool(self) -> None:
        pool_cfg = self.config["pool"]
        self.pool = ConnectionPool(
            self.dsn,
            min_size=int(pool_cfg["min_size"]),
            max_size=int(pool_cfg["max_size"]),
            timeout=float(pool_cfg["timeout_s"]),
            kwargs={"row_factory": dict_row},
            open=False,
            name="geoqc-api",
        )
        # Do not wait: the API must start even when PostGIS is down (health reports db=false).
        self.pool.open(wait=False)

    def close_pool(self) -> None:
        if self.pool is not None:
            self.pool.close()
            self.pool = None

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[psycopg.Connection]:
        """Yield a pooled connection (committed on success); 503 when the DB is unreachable."""
        if self.pool is None:
            raise HTTPException(status_code=503, detail="database pool is not open")
        try:
            with self.pool.connection(timeout=timeout) as conn:
                yield conn
        except (PoolTimeout, psycopg.OperationalError) as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc

    def db_ok(self, timeout: float = 2.0) -> bool:
        if self.pool is None:
            return False
        try:
            with self.pool.connection(timeout=timeout) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception as exc:
            log.warning("database check failed: %s", exc)
            return False

    def has_matching_tables(self, conn: psycopg.Connection) -> bool:
        """True when sql/002_matching.sql (det_suppressed, discrepancy_features) is applied.

        Cached after the first positive answer; a negative answer is re-checked on every call
        so that applying the migration while the API runs is picked up.
        """
        if self._matching_tables:
            return True
        row = conn.execute(
            "SELECT to_regclass('det_suppressed') IS NOT NULL"
            " AND to_regclass('discrepancy_features') IS NOT NULL AS ok"
        ).fetchone()
        self._matching_tables = bool(row and row["ok"])
        return self._matching_tables

    # -- convenience -------------------------------------------------------------------------
    @property
    def esri_enabled(self) -> bool:
        return bool(self.arcgis_api_key)

    def cache_header(self, kind: str) -> str:
        seconds = int(self.config["cache_seconds"].get(kind, 0))
        return f"public, max-age={seconds}" if seconds > 0 else "no-store"


def get_ctx(request: Request) -> AppContext:
    """FastAPI dependency returning the application context."""
    return request.app.state.ctx
