"""CRS helpers: UTM zone selection (docs/CONTRACTS.md §3.1)."""

from __future__ import annotations

import math


def utm_epsg_for(lon: float, lat: float) -> int:
    """Return the EPSG code of the WGS84 / UTM zone containing ``(lon, lat)``.

    Northern hemisphere -> 326xx, southern -> 327xx. Norway/Svalbard exceptions
    of the UTM grid are intentionally ignored: the plain 6-degree formula keeps
    the metric frame predictable for any AOI and matches what GDAL/pyproj use
    for ``+proj=utm``.
    """
    if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
        raise ValueError(f"lon/lat out of range: {lon}, {lat}")
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    zone = min(max(zone, 1), 60)
    base = 32600 if lat >= 0 else 32700
    return base + zone


def utm_epsg_for_bbox(bbox: tuple[float, float, float, float]) -> int:
    """UTM zone of the bbox centre (``minx, miny, maxx, maxy`` in EPSG:4326)."""
    minx, miny, maxx, maxy = bbox
    return utm_epsg_for((minx + maxx) / 2.0, (miny + maxy) / 2.0)
