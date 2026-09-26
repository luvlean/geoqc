"""Configuration loader shared by all modules.

Merges ``config/pipeline.yaml`` (global) with every ``config/<module>.yaml``
into one nested dict; environment settings come from ``.env`` via pydantic-settings.
Module owners edit only their own ``config/<module>.yaml`` (see docs/CONTRACTS.md §5).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


class Settings(BaseSettings):
    """Environment-driven settings (secrets never live in yaml)."""

    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str = "postgresql://geoqc:geoqc@localhost:5433/geoqc"
    test_database_url: str = "postgresql://geoqc:geoqc@localhost:5433/geoqc_test"
    arcgis_api_key: str = ""
    geoqc_data_dir: str = "data"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at top level")
    return data


def load_config(config_dir: Path | None = None) -> dict[str, Any]:
    """Return ``{**pipeline.yaml, "<module>": <module>.yaml, ..., "gates": quality_gates}``."""
    cfg_dir = Path(config_dir or os.environ.get("GEOQC_CONFIG_DIR", CONFIG_DIR))
    cfg = _read_yaml(cfg_dir / "pipeline.yaml")
    for path in sorted(cfg_dir.glob("*.yaml")):
        if path.name in {"pipeline.yaml", "quality_gates.yaml"}:
            continue
        cfg[path.stem] = _read_yaml(path)
    gates_path = cfg_dir / "quality_gates.yaml"
    if gates_path.exists():
        cfg["gates"] = _read_yaml(gates_path).get("gates", {})
    return cfg


def repo_relative(value: Any) -> Any:
    """Absolute paths inside the repo -> repo-relative, recursively (for committed reports)."""
    if isinstance(value, dict):
        return {k: repo_relative(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [repo_relative(v) for v in value]
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str) and value.startswith(str(REPO_ROOT) + "/"):
        return Path(value).relative_to(REPO_ROOT).as_posix()
    return value


def resolve_path(rel: str | os.PathLike[str]) -> Path:
    """Resolve a repo-relative path from config to an absolute path."""
    p = Path(rel)
    return p if p.is_absolute() else REPO_ROOT / p
