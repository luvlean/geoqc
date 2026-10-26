"""Pydantic models and query-parameter parsers for the API (docs/CONTRACTS.md §8, §3.8)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from geoqc.api.deps import DISCREPANCY_CLASSES, DISCREPANCY_STATUSES

DiscrepancyClass = Literal["missing_in_map", "missing_on_image", "geometry_mismatch"]
DiscrepancyStatus = Literal["new", "confirmed", "rejected"]


class BBox(BaseModel):
    """WGS84 bounding box ``minx,miny,maxx,maxy``."""

    minx: float
    miny: float
    maxx: float
    maxy: float

    @classmethod
    def parse(cls, text: str) -> BBox:
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 4:
            raise ValueError("bbox must have exactly 4 comma-separated numbers")
        try:
            minx, miny, maxx, maxy = (float(p) for p in parts)
        except ValueError as exc:
            raise ValueError("bbox values must be numbers") from exc
        if not (-180.0 <= minx <= 180.0 and -180.0 <= maxx <= 180.0):
            raise ValueError("bbox longitudes must be within [-180, 180]")
        if not (-90.0 <= miny <= 90.0 and -90.0 <= maxy <= 90.0):
            raise ValueError("bbox latitudes must be within [-90, 90]")
        if minx >= maxx or miny >= maxy:
            raise ValueError("bbox must satisfy minx < maxx and miny < maxy")
        return cls(minx=minx, miny=miny, maxx=maxx, maxy=maxy)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.minx, self.miny, self.maxx, self.maxy)


def parse_csv_enum(text: str | None, allowed: tuple[str, ...], name: str) -> list[str] | None:
    """Split ``a,b`` and validate each value against ``allowed``; None/empty -> None (no filter)."""
    if text is None or not text.strip():
        return None
    values = [v.strip() for v in text.split(",") if v.strip()]
    bad = [v for v in values if v not in allowed]
    if bad:
        raise ValueError(
            f"unknown {name} value(s): {', '.join(bad)}; allowed: {', '.join(allowed)}"
        )
    return list(dict.fromkeys(values))


class DiscrepancyFilter(BaseModel):
    """Validated filters shared by list and export endpoints."""

    bbox: BBox | None = None
    classes: list[str] | None = None
    min_conf: float | None = Field(default=None, ge=0.0, le=1.0)
    statuses: list[str] | None = None

    @field_validator("classes")
    @classmethod
    def _check_classes(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and any(c not in DISCREPANCY_CLASSES for c in v):
            raise ValueError("unknown class")
        return v

    @field_validator("statuses")
    @classmethod
    def _check_statuses(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and any(s not in DISCREPANCY_STATUSES for s in v):
            raise ValueError("unknown status")
        return v


class DiscrepancyProperties(BaseModel):
    """Feature properties returned by ``/discrepancies`` (superset of the export contract §3.8)."""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    class_: DiscrepancyClass = Field(alias="class")
    confidence: float
    iou: float | None = None
    status: DiscrepancyStatus
    ref_id: int | None = None
    det_id: int | None = None
    ref_osm_id: int | None = None
    model_version: str | None = None
    created_at: datetime
    updated_at: datetime


class ExportProperties(BaseModel):
    """Exactly the property set of docs/CONTRACTS.md §3.8 (MapRoulette / JOSM friendly)."""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    class_: DiscrepancyClass = Field(alias="class")
    confidence: float
    iou: float | None = None
    status: DiscrepancyStatus
    ref_osm_id: int | None = None
    model_version: str | None = None
    created_at: datetime


class Feature(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: int
    geometry: dict[str, Any]
    properties: dict[str, Any]


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[Feature]
    numberReturned: int  # noqa: N815 - OGC API Features naming
    numberMatched: int | None = None  # noqa: N815


class StatusPatch(BaseModel):
    status: DiscrepancyStatus


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    db: bool


class Stats(BaseModel):
    total: int
    by_class: dict[str, int]
    by_status: dict[str, int]
    by_class_status: dict[str, dict[str, int]]
    model_versions: list[str]
    buildings: dict[str, int]  # ref, det (without NMS-suppressed), det_suppressed


class AoiInfo(BaseModel):
    name: str | None = None
    center: list[float] | None = None  # [lon, lat]
    bbox: list[float] | None = None  # [minx, miny, maxx, maxy]
    area_km2: float | None = None
    utm_epsg: int | None = None


class ImageInfo(BaseModel):
    source: str | None = None
    id: str | None = None
    acquired: str | None = None
    license: str | None = None
    gsd_m: float | None = None
    url: str | None = None


class Basemap(BaseModel):
    key: str
    title: str
    url: str
    attribution: str
    max_zoom: int = 19
    tms: bool = False


class MapConfig(BaseModel):
    aoi: AoiInfo
    image: ImageInfo | None = None
    attribution: dict[str, str | None]
    basemaps: list[Basemap]
    imagery_available: bool
    esri_enabled: bool
    review_tools: bool = False
    osm_snapshot_date: str | None = None
    classes: list[str] = list(DISCREPANCY_CLASSES)
    statuses: list[str] = list(DISCREPANCY_STATUSES)
    mvt_layers: dict[str, int]
    default_limit: int
    max_limit: int
    note: str | None = None
