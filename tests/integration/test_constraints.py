"""Статические проверки ограничений плана: тестовая разметка, секреты, запрещённые подложки, схема БД."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {".py", ".ipynb", ".yaml", ".yml", ".sql", ".md", ".toml", ".js", ".html", ".sh"}
# basemap-only providers (D-002): never a raster source for detection or training
FORBIDDEN_TILE_HOSTS = re.compile(
    r"arcgisonline|virtualearth|tiles\.bing|mt\d?\.google|khms\d?\.google|maps\.yandex",
    re.IGNORECASE,
)


def _files(*rel: str) -> list[Path]:
    out: list[Path] = []
    for r in rel:
        p = ROOT / r
        out += [f for f in p.rglob("*") if f.is_file() and f.suffix in TEXT_SUFFIXES]
    return out


def _tracked() -> list[str]:
    try:
        res = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return res.stdout.splitlines()


@pytest.mark.parametrize("area", ["src/geoqc/cv", "src/geoqc/matching", "notebooks"])
def test_no_test_labels_path(area):
    """CV, matching и ноутбук не ссылаются на data/labels/test."""
    hits = [str(f.relative_to(ROOT)) for f in _files(area) if "labels/test" in f.read_text("utf-8")]
    assert hits == []


@pytest.mark.parametrize("area", ["src/geoqc/data", "src/geoqc/cv", "notebooks"])
def test_no_basemap_tiles_as_raster_source(area):
    """В data/cv/ноутбуке нет URL тайлов Esri/Bing/Google/Yandex."""
    hits = [
        str(f.relative_to(ROOT))
        for f in _files(area)
        if FORBIDDEN_TILE_HOSTS.search(f.read_text("utf-8"))
    ]
    assert hits == []


def test_no_env_file_tracked():
    """.env не попадает в git, .env.example — попадает."""
    tracked = _tracked()
    assert not [f for f in tracked if Path(f).name == ".env"]
    assert ".env.example" in tracked


def test_no_secret_values_tracked():
    """В отслеживаемых файлах нет непустых ключей API."""
    pattern = re.compile(
        r"^[ \t]*(ARCGIS_API_KEY|[A-Z_]*SECRET[A-Z_]*|[A-Z_]*TOKEN)[ \t]*=[ \t]*[^\s#]+", re.M
    )
    hits = []
    for rel in _tracked():
        f = ROOT / rel
        if f.suffix in TEXT_SUFFIXES | {".example", ""} and f.is_file():
            try:
                text = f.read_text("utf-8")
            except UnicodeDecodeError:
                continue
            hits += [f"{rel}: {m.group(1)}" for m in pattern.finditer(text)]
    assert hits == []


def test_schema_matches_contract():
    """sql/001_schema.sql содержит ровно таблицы, классы и статусы CONTRACTS §3.7."""
    sql = (ROOT / "sql" / "001_schema.sql").read_text("utf-8")
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
    assert tables == {"buildings_ref", "buildings_det", "matches", "discrepancies"}
    assert "'missing_in_map', 'missing_on_image', 'geometry_mismatch'" in sql
    assert "'new', 'confirmed', 'rejected'" in sql
    assert "UNIQUE (ref_id, model_version)" in sql and "UNIQUE (det_id, model_version)" in sql
