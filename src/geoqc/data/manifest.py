"""Read/write ``data/manifest.json`` (docs/CONTRACTS.md §3.4) with partial updates."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geoqc.config import repo_relative, resolve_path

MANIFEST_PATH = resolve_path("data/manifest.json")

_EMPTY: dict[str, Any] = {
    "aoi": {"name": None, "area_km2": None, "utm_epsg": None, "bbox_wgs84": None},
    "image": {
        "source": None,
        "id": None,
        "url": None,
        "acquired": None,
        "license": None,
        "attribution": None,
        "sha256": None,
        "gsd_m": None,
        "crs": None,
    },
    "osm": {"snapshot_date": None, "query": None, "count": None, "license": "ODbL"},
    "secondary": {"source": None, "url": None, "count": 0, "license": None},
    "georef": {"n_control_points": 0, "median_offset_m": None, "shift_applied_m": [0.0, 0.0]},
    "created_at": None,
}


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """Return the manifest dict; an empty skeleton when the file does not exist."""
    if not path.exists():
        return json.loads(json.dumps(_EMPTY))
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    # Fill missing top-level sections so callers can rely on the contract keys.
    for key, value in _EMPTY.items():
        if key not in data:
            data[key] = json.loads(json.dumps(value))
    return data


def save_manifest(data: dict[str, Any], path: Path = MANIFEST_PATH) -> Path:
    data = repo_relative(data)
    data["created_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def update_manifest(
    section: str, values: dict[str, Any], path: Path = MANIFEST_PATH
) -> dict[str, Any]:
    """Merge ``values`` into ``manifest[section]`` and persist."""
    data = load_manifest(path)
    current = data.get(section) or {}
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    data[section] = current
    save_manifest(data, path)
    return data


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
