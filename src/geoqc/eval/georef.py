"""Read ``reports/georef.json`` (Data Agent) into the ``georeference`` block of metrics.json (G1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from geoqc.eval.common import pick, read_json, rel

# Accepted key spellings, most specific first. "after correction" values win over raw ones (G1 is
# measured after the shift has been applied, plan §6).
_OFFSET_KEYS = (
    "median_offset_after_m",
    "after.median_offset_m",
    "median_offset_m_after",
    "georef.median_offset_after_m",
    "median_offset_m",
    "georef.median_offset_m",
    "median_offset",
    "offset.median_m",
)
_NPOINTS_KEYS = (
    "n_control_points",
    "georef.n_control_points",
    "n_points",
    "control_points.n",
    "n",
)


def read_georef(path: Path | None, manifest_path: Path | None = None) -> dict[str, Any]:
    """Return ``{"median_offset_m", "n_control_points", "source", "note"}``.

    Falls back to ``data/manifest.json -> georef`` when the report is missing. Values are None
    when nothing is available; ``note`` explains why.
    """
    data = read_json(path)
    source: str | None = str(path) if data is not None else None
    if data is None:
        manifest = read_json(manifest_path)
        data = (manifest or {}).get("georef") if manifest else None
        source = f"{manifest_path} -> georef" if data else None
    if not data:
        return {
            "median_offset_m": None,
            "n_control_points": None,
            "source": None,
            "note": f"{rel(path) if path else 'reports/georef.json'} not found (run `geoqc data check-georef`)",
        }
    offset = pick(data, *_OFFSET_KEYS)
    n_points = pick(data, *_NPOINTS_KEYS)
    note = None
    if offset is None:
        note = f"no median offset key in {source}; expected one of {_OFFSET_KEYS[:2]}"
    return {
        "median_offset_m": float(offset) if offset is not None else None,
        "n_control_points": int(n_points) if n_points is not None else None,
        "source": source,
        "note": note,
    }
