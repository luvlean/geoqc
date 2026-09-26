"""``geoqc cv bundle``: zip the material needed by the Colab notebook.

Contents (repo-relative paths inside the archive): ``data/tiles/*.tif`` (all tiles or only
val tiles), ``data/tiles/tiles.gpkg``, ``data/labels/val/*.geojson``,
``data/labels/tiles_split.csv``, ``data/manifest.json``, ``config/*.yaml``,
``config/aoi.geojson``, the package source ``src/geoqc/**/*.py`` + ``pyproject.toml`` +
``requirements.txt`` (so Colab can ``sys.path``-import ``geoqc``) and, when present,
``data/interim/dataset/**``. The test-split labels directory is never added; the archive is
verified against that before it is returned.
"""

from __future__ import annotations

import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from geoqc.config import REPO_ROOT, resolve_path
from geoqc.cv.dataset import FORBIDDEN_SPLIT, LABEL_SPLIT
from geoqc.cv.tiles import filter_by_split, load_tile_index


@dataclass
class BundleSummary:
    path: str
    n_files: int
    n_tiles: int
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_forbidden_arcname(arcname: str) -> bool:
    parts = [p.lower() for p in Path(arcname).parts]
    return any(
        a == "labels" and b == FORBIDDEN_SPLIT for a, b in zip(parts, parts[1:], strict=False)
    )


def _arcname(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return Path("external", path.name).as_posix()


def build_bundle(
    cv_cfg: dict[str, Any],
    pipeline_cfg: dict[str, Any] | None = None,
    *,
    out: str | Path | None = None,
    tiles: str | None = None,
    tiles_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    labels_dir: str | Path | None = None,
    split_csv: str | Path | None = None,
    config_dir: str | Path | None = None,
    dataset_dir: str | Path | None = None,
    manifest: str | Path | None = None,
    include_dataset: bool = True,
    include_source: bool = True,
    root: str | Path | None = None,
) -> BundleSummary:
    b_cfg = dict(cv_cfg.get("bundle") or {})
    tiles_mode = (tiles or b_cfg.get("tiles") or "all").lower()
    if tiles_mode not in {"all", LABEL_SPLIT}:
        raise ValueError(f"tiles must be 'all' or '{LABEL_SPLIT}'")
    root = Path(resolve_path(root or REPO_ROOT))
    paths = (pipeline_cfg or {}).get("paths", {}) if pipeline_cfg else {}
    tiles_dir_p = Path(resolve_path(tiles_dir or paths.get("tiles", "data/tiles")))
    labels_dir_p = Path(resolve_path(labels_dir or paths.get("labels", "data/labels")))
    split_csv_p = Path(resolve_path(split_csv or labels_dir_p / "tiles_split.csv"))
    config_dir_p = Path(resolve_path(config_dir or "config"))
    dataset_dir_p = Path(
        resolve_path(
            dataset_dir or (cv_cfg.get("dataset") or {}).get("out_dir", "data/interim/dataset")
        )
    )
    manifest_p = Path(resolve_path(manifest or "data/manifest.json"))
    out_p = Path(resolve_path(out or b_cfg.get("out", "data/interim/bundle.zip")))

    files: list[Path] = []
    entries: list[tuple[Path, str]] = []  # (file, arcname) for files outside ``root``
    index = load_tile_index(tiles_dir_p, index_path)
    if tiles_mode == LABEL_SPLIT and split_csv_p.exists():
        index = filter_by_split(index, LABEL_SPLIT, split_csv_p)
    tile_paths = [Path(p) for p in index["path"]] if len(index) else []
    files += [p for p in tile_paths if p.exists()]
    gpkg = Path(resolve_path(index_path)) if index_path else tiles_dir_p / "tiles.gpkg"
    if gpkg.exists():
        files.append(gpkg)
    val_dir = labels_dir_p / LABEL_SPLIT
    if val_dir.is_dir():
        files += sorted(p for p in val_dir.glob("*.geojson") if p.is_file())
    if split_csv_p.exists():
        files.append(split_csv_p)
    if manifest_p.exists():
        files.append(manifest_p)
    if config_dir_p.is_dir():
        files += sorted(p for p in config_dir_p.glob("*.yaml") if p.is_file())
        aoi = config_dir_p / "aoi.geojson"
        if aoi.exists():
            files.append(aoi)
    if include_dataset and dataset_dir_p.is_dir():
        files += sorted(p for p in dataset_dir_p.rglob("*") if p.is_file())
    if include_source:
        src_root = REPO_ROOT / "src" / "geoqc"
        for f in sorted(src_root.rglob("*.py")):
            if "__pycache__" not in f.parts:
                entries.append((f, _arcname(f, REPO_ROOT)))
        for name in ("pyproject.toml", "requirements.txt"):
            if (REPO_ROOT / name).exists():
                entries.append((REPO_ROOT / name, name))

    out_p.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    with zipfile.ZipFile(out_p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f, arc in [(f, _arcname(f, root)) for f in files] + entries:
            if arc in seen:
                continue
            if is_forbidden_arcname(arc):
                raise PermissionError(f"refusing to bundle {arc}")
            zf.write(f, arc)
            seen.add(arc)
    with zipfile.ZipFile(out_p) as zf:
        names = zf.namelist()
    bad = [n for n in names if is_forbidden_arcname(n)]
    if bad:
        out_p.unlink(missing_ok=True)
        raise PermissionError(f"bundle contained forbidden entries: {bad}")
    return BundleSummary(
        path=str(out_p),
        n_files=len(names),
        n_tiles=len([p for p in tile_paths if p.exists()]),
        size_bytes=out_p.stat().st_size,
    )
