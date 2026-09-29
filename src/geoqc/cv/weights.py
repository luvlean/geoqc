"""Weights download, hashing and ``model_version`` bookkeeping.

``geoqc cv fetch-weights`` tries the Hugging Face Hub checkpoint named in ``config/cv.yaml``
(``weights.hf_repo`` / ``weights.hf_filename``). If that fails and ``allow_fallback`` is set,
it downloads the Ultralytics COCO checkpoint ``weights.fallback_asset`` (architecture only;
it has *no* building class and cannot detect buildings without fine-tuning - the model card
says so explicitly). Either way ``model_version = "<model_name>-<sha256[:8]>"`` is written back
into ``config/cv.yaml`` and into the auto block of ``models/MODEL_CARD.md``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from geoqc.config import CONFIG_DIR, REPO_ROOT, resolve_path

CARD_BEGIN = "<!-- weights:begin -->"
CARD_END = "<!-- weights:end -->"

# Known provenance for the checkpoints this module can fetch (URL + licence as published).
KNOWN_SOURCES: dict[str, dict[str, str]] = {
    "keremberke/yolov8m-building-segmentation": {
        "url": "https://huggingface.co/keremberke/yolov8m-building-segmentation",
        "license": (
            "no licence tag on the model repo; derivative of Ultralytics yolov8m-seg "
            "(AGPL-3.0); training data Roboflow 'Buildings Instance Segmentation' (CC BY 4.0)"
        ),
    },
    "yolov8m-seg.pt": {
        "url": "https://github.com/ultralytics/assets/releases (yolov8m-seg.pt, COCO)",
        "license": "AGPL-3.0 (Ultralytics)",
    },
}


@dataclass
class WeightsInfo:
    path: str
    sha256: str
    model_name: str
    model_version: str
    source_url: str
    license: str
    is_fallback: bool
    backend: str
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_of(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def model_version_for(model_name: str, sha256: str) -> str:
    """Contract §3.6: ``"<model_name>-<weights_sha256[:8]>"``."""
    if not model_name or not sha256:
        raise ValueError("model_name and sha256 are required")
    return f"{model_name}-{sha256[:8]}"


def _download_hf(repo_id: str, filename: str, dest: Path) -> Path:
    from huggingface_hub import hf_hub_download

    cached = Path(hf_hub_download(repo_id=repo_id, filename=filename))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, dest)
    return dest


def _download_ultralytics_asset(asset: str, dest_dir: Path) -> Path:
    from ultralytics.utils.downloads import attempt_download_asset

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / asset
    got = attempt_download_asset(str(target))
    got_path = Path(got) if got else target
    if got_path.resolve() != target.resolve() and got_path.exists():
        shutil.copyfile(got_path, target)
    if not target.exists():
        raise FileNotFoundError(f"ultralytics asset {asset} was not downloaded")
    return target


def fetch_weights(
    cv_cfg: dict[str, Any],
    *,
    force: bool = False,
    allow_fallback: bool = True,
    models_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    card_path: str | Path | None = None,
    update_files: bool = True,
) -> WeightsInfo:
    """Ensure weights exist locally, hash them, write model_version to config and model card."""
    w = dict(cv_cfg.get("weights") or {})
    model_name = str(cv_cfg.get("model_name", "yolov8m-building-seg"))
    models_dir = Path(resolve_path(models_dir or "models"))
    dest = Path(resolve_path(w.get("path") or f"models/{model_name}.pt"))
    repo, filename = w.get("hf_repo"), w.get("hf_filename", "best.pt")
    fallback_asset = w.get("fallback_asset", "yolov8m-seg.pt")

    is_fallback = False
    downloaded = False
    source = KNOWN_SOURCES.get(
        repo or "", {"url": f"https://huggingface.co/{repo}", "license": "unknown"}
    )
    if dest.exists() and not force:
        is_fallback = bool(w.get("is_fallback", False))
        if is_fallback:
            source = KNOWN_SOURCES.get(
                fallback_asset, {"url": "ultralytics assets", "license": "AGPL-3.0"}
            )
    else:
        try:
            if not repo:
                raise ValueError("weights.hf_repo is not set")
            _download_hf(repo, filename, dest)
            downloaded = True
        except Exception as exc:
            if not allow_fallback:
                raise
            asset_path = _download_ultralytics_asset(fallback_asset, models_dir)
            dest = asset_path
            downloaded = True
            is_fallback = True
            model_name = Path(fallback_asset).stem + "-coco"
            source = KNOWN_SOURCES.get(
                fallback_asset, {"url": "ultralytics assets", "license": "AGPL-3.0"}
            )
            source = {**source, "note": f"HF download failed: {type(exc).__name__}: {exc}"}

    sha = sha256_of(dest)
    info = WeightsInfo(
        path=str(dest.relative_to(REPO_ROOT)) if dest.is_relative_to(REPO_ROOT) else str(dest),
        sha256=sha,
        model_name=model_name,
        model_version=model_version_for(model_name, sha),
        source_url=source.get("url", ""),
        license=source.get("license", "unknown"),
        is_fallback=is_fallback,
        backend=str(cv_cfg.get("backend", "ultralytics")),
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    )
    # a fine-tune export already wrote config and card for this file: keep its provenance
    recorded = w.get("sha256") == sha and cv_cfg.get("model_version") == info.model_version
    if update_files and (downloaded or not recorded):
        update_cv_config(
            {
                "model_name": info.model_name,
                "model_version": info.model_version,
                "weights.path": info.path,
                "weights.sha256": info.sha256,
                "weights.is_fallback": info.is_fallback,
            },
            config_path=config_path,
        )
        update_model_card(info, card_path=card_path)
    return info


def update_cv_config(updates: dict[str, Any], config_path: str | Path | None = None) -> Path:
    """Round-trip edit of ``config/cv.yaml`` (comments preserved). Keys may be dotted."""
    from ruamel.yaml import YAML

    path = Path(config_path) if config_path else CONFIG_DIR / "cv.yaml"
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    data = yaml.load(path.read_text(encoding="utf-8")) or {}
    for key, value in updates.items():
        node = data
        parts = key.split(".")
        for p in parts[:-1]:
            if p not in node or node[p] is None:
                node[p] = {}
            node = node[p]
        node[parts[-1]] = value
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    return path


def render_card_block(info: WeightsInfo, extra: dict[str, Any] | None = None) -> str:
    rows = {
        "model_version": f"`{info.model_version}`",
        "backend": info.backend,
        "weights": f"`{info.path}`",
        "sha256": f"`{info.sha256}`",
        "source": info.source_url,
        "license": info.license,
        "fallback (COCO, no building class)": "yes" if info.is_fallback else "no",
        "fetched_at": info.fetched_at,
    }
    if extra:
        rows.update({k: str(v) for k, v in extra.items()})
    lines = [CARD_BEGIN, "| Поле | Значение |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows.items()]
    lines.append(CARD_END)
    return "\n".join(lines)


def update_model_card(
    info: WeightsInfo,
    card_path: str | Path | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Replace the auto-generated block between ``CARD_BEGIN``/``CARD_END`` in MODEL_CARD.md."""
    path = Path(card_path) if card_path else REPO_ROOT / "models" / "MODEL_CARD.md"
    block = render_card_block(info, extra)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if CARD_BEGIN in text and CARD_END in text:
            start = text.index(CARD_BEGIN)
            end = text.index(CARD_END) + len(CARD_END)
            text = text[:start] + block + text[end:]
        else:
            text = text.rstrip("\n") + "\n\n## Текущие веса (автоблок)\n\n" + block + "\n"
    else:
        text = "# Карточка модели\n\n## Текущие веса (автоблок)\n\n" + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def current_model_version(cv_cfg: dict[str, Any]) -> str:
    """model_version from config, or recomputed from the weights file when missing."""
    mv = cv_cfg.get("model_version")
    if mv:
        return str(mv)
    backend = str(cv_cfg.get("backend", "ultralytics"))
    if backend == "mock":
        return "mock-00000000"
    if backend == "smp_unet":
        path = Path(resolve_path((cv_cfg.get("smp_unet") or {}).get("checkpoint", "")))
        name = f"unet-{(cv_cfg.get('smp_unet') or {}).get('encoder', 'resnet34')}"
    else:
        path = Path(resolve_path((cv_cfg.get("weights") or {}).get("path", "")))
        name = str(cv_cfg.get("model_name", "yolov8m-building-seg"))
    if path.exists():
        return model_version_for(name, sha256_of(path))
    raise FileNotFoundError(
        "model_version is not set in config/cv.yaml and weights are missing; "
        "run `geoqc cv fetch-weights`"
    )
