"""weights: sha256 -> model_version, правка config/cv.yaml и автоблока карточки без сети."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from geoqc.cv.weights import (
    CARD_BEGIN,
    CARD_END,
    WeightsInfo,
    current_model_version,
    fetch_weights,
    model_version_for,
    sha256_of,
    update_cv_config,
    update_model_card,
)


def test_model_version_format(tmp_path: Path):
    """model_version = '<model_name>-<sha256[:8]>' по §3.6; sha256 совпадает с hashlib."""
    f = tmp_path / "w.pt"
    f.write_bytes(b"weights" * 100)
    sha = sha256_of(f)
    import hashlib

    assert sha == hashlib.sha256(b"weights" * 100).hexdigest()
    assert model_version_for("yolov8m-building-seg", sha) == f"yolov8m-building-seg-{sha[:8]}"
    with pytest.raises(ValueError):
        model_version_for("", sha)


def test_update_cv_config_round_trip_keeps_comments(tmp_path: Path):
    """Точечная правка ключей (в т. ч. вложенных через точку) сохраняет комментарии YAML."""
    cfg = tmp_path / "cv.yaml"
    cfg.write_text(
        "# keep me\nbackend: ultralytics\nweights:\n  path: models/a.pt  # inline\n  sha256: null\nmodel_version: null\n",
        encoding="utf-8",
    )
    update_cv_config(
        {"weights.sha256": "abc", "model_version": "m-abc", "conf_threshold": 0.35}, config_path=cfg
    )
    text = cfg.read_text()
    assert "# keep me" in text and "# inline" in text
    data = yaml.safe_load(text)
    assert (
        data["weights"]["sha256"] == "abc"
        and data["model_version"] == "m-abc"
        and data["conf_threshold"] == 0.35
    )


def test_update_model_card_block(tmp_path: Path):
    """Автоблок между маркерами заменяется, остальной текст карточки не меняется."""
    card = tmp_path / "MODEL_CARD.md"
    card.write_text(
        f"# Card\n\nintro\n\n{CARD_BEGIN}\nold\n{CARD_END}\n\noutro\n", encoding="utf-8"
    )
    info = WeightsInfo(
        "models/x.pt",
        "f" * 64,
        "x",
        "x-ffffffff",
        "https://example",
        "MIT",
        False,
        "ultralytics",
        "2026-01-01T00:00:00+00:00",
    )
    update_model_card(info, card_path=card)
    text = card.read_text()
    assert "old" not in text and "x-ffffffff" in text and "intro" in text and "outro" in text
    assert text.count(CARD_BEGIN) == 1 and text.count(CARD_END) == 1
    # card without markers gets the block appended
    card2 = tmp_path / "no_markers.md"
    card2.write_text("# Card\n")
    update_model_card(info, card_path=card2)
    assert CARD_BEGIN in card2.read_text()


def test_fetch_weights_uses_existing_file_without_network(tmp_path: Path):
    """Если файл весов уже есть, fetch-weights не ходит в сеть: хэширует и пишет model_version в конфиг/карточку."""
    w = tmp_path / "models" / "yolov8m-building-seg.pt"
    w.parent.mkdir()
    w.write_bytes(b"\x00" * 1024)
    cfg = tmp_path / "cv.yaml"
    cfg.write_text(
        "backend: ultralytics\nmodel_name: yolov8m-building-seg\nweights:\n  path: x\n  hf_repo: keremberke/yolov8m-building-segmentation\n  sha256: null\nmodel_version: null\n"
    )
    card = tmp_path / "MODEL_CARD.md"
    cv_cfg = {
        "backend": "ultralytics",
        "model_name": "yolov8m-building-seg",
        "weights": {
            "path": str(w),
            "hf_repo": "keremberke/yolov8m-building-segmentation",
            "hf_filename": "best.pt",
        },
    }
    info = fetch_weights(cv_cfg, config_path=cfg, card_path=card, models_dir=w.parent)
    assert info.sha256 == sha256_of(w) and info.model_version.endswith(info.sha256[:8])
    assert not info.is_fallback and "huggingface.co" in info.source_url
    data = yaml.safe_load(cfg.read_text())
    assert data["model_version"] == info.model_version and data["weights"]["sha256"] == info.sha256
    assert info.model_version in card.read_text()


def test_current_model_version_from_config_or_file(tmp_path: Path):
    """current_model_version берёт значение из конфига, иначе пересчитывает по файлу, иначе ошибка."""
    assert current_model_version({"model_version": "abc-12345678"}) == "abc-12345678"
    assert current_model_version({"backend": "mock"}) == "mock-00000000"
    w = tmp_path / "w.pt"
    w.write_bytes(b"1")
    mv = current_model_version(
        {"backend": "ultralytics", "model_name": "m", "weights": {"path": str(w)}}
    )
    assert mv == f"m-{sha256_of(w)[:8]}"
    with pytest.raises(FileNotFoundError):
        current_model_version(
            {"backend": "ultralytics", "weights": {"path": str(tmp_path / "nope.pt")}}
        )


def test_fetch_weights_keeps_recorded_fine_tune(tmp_path: Path):
    """Веса уже записаны в конфиг (экспорт дообучения): fetch-weights не трогает конфиг и карточку."""
    w = tmp_path / "ft.pt"
    w.write_bytes(b"ft" * 512)
    sha = sha256_of(w)
    cfg, card = tmp_path / "cv.yaml", tmp_path / "MODEL_CARD.md"
    cfg.write_text("model_name: m-ft  # keep\n")
    card.write_text(f"{CARD_BEGIN}\nfine-tuned in notebook\n{CARD_END}\n")
    cv_cfg = {
        "model_name": "m-ft",
        "model_version": model_version_for("m-ft", sha),
        "weights": {"path": str(w), "hf_repo": "r/x", "sha256": sha},
    }
    info = fetch_weights(cv_cfg, config_path=cfg, card_path=card, models_dir=tmp_path)
    assert info.model_version == cv_cfg["model_version"]
    assert cfg.read_text() == "model_name: m-ft  # keep\n"
    assert "fine-tuned in notebook" in card.read_text()


def test_fetch_weights_cli_skips_non_ultralytics_backend(monkeypatch: pytest.MonkeyPatch):
    """При backend=smp_unet команда fetch-weights ничего не скачивает и не пересчитывает model_version."""
    from typer.testing import CliRunner

    from geoqc.cv import cli, weights

    monkeypatch.setattr(cli, "load_config", lambda: {"cv": {"backend": "smp_unet"}})
    monkeypatch.setattr(weights, "fetch_weights", lambda *a, **k: pytest.fail("fetch called"))
    res = CliRunner().invoke(cli.app, ["fetch-weights"])
    assert res.exit_code == 0 and "nothing to fetch" in res.output
