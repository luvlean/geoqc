"""Манифест коммитится: пути внутри репозитория пишутся относительными."""

from __future__ import annotations

import json

from geoqc.config import REPO_ROOT
from geoqc.data.manifest import save_manifest


def test_save_manifest_relativizes_repo_paths(tmp_path):
    """Абсолютные пути внутри репо становятся относительными, внешние и URL не меняются."""
    out = tmp_path / "manifest.json"
    save_manifest(
        {
            "image": {"raw_path": str(REPO_ROOT / "data/raw/x.tif"), "url": "https://a/b.tif"},
            "interim": {"aoi_cog": str(REPO_ROOT / "data/interim/aoi_cog.tif")},
            "other": ["/opt/elsewhere/file.tif", str(REPO_ROOT / "data/tiles/tiles.gpkg")],
        },
        out,
    )
    data = json.loads(out.read_text())
    assert data["image"] == {"raw_path": "data/raw/x.tif", "url": "https://a/b.tif"}
    assert data["interim"]["aoi_cog"] == "data/interim/aoi_cog.tif"
    assert data["other"] == ["/opt/elsewhere/file.tif", "data/tiles/tiles.gpkg"]
