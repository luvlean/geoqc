"""scripts/render_metrics_table.py: подстановка {{metrics.*}} в главы записки и таблица гейтов."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_metrics_table.py"


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *argv])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    return int(exc.value.code)


def test_fill_substitutes_values_and_reports_missing_keys(tmp_path, monkeypatch, capsys):
    """Известные ключи подставляются (float — 3 знака, bool — да/нет), неизвестные дают n/a и код 1."""
    metrics = tmp_path / "metrics.json"
    metrics.write_text(
        json.dumps({"detection": {"test": {"f1": 0.81234}}, "gates": {"G4": {"passed": True}}}),
        encoding="utf-8",
    )
    doc = tmp_path / "ch.md"
    doc.write_text(
        "F1 {{metrics.detection.test.f1}}, G4 {{metrics.gates.G4.passed}}", encoding="utf-8"
    )
    out = tmp_path / "out"
    assert (
        _run(monkeypatch, "fill", str(doc), "--metrics", str(metrics), "--out-dir", str(out)) == 0
    )
    assert (out / "ch.md").read_text(encoding="utf-8") == "F1 0.812, G4 да"
    doc.write_text("{{metrics.nope.x}}", encoding="utf-8")
    assert (
        _run(monkeypatch, "fill", str(doc), "--metrics", str(metrics), "--out-dir", str(out)) == 1
    )
    assert (out / "ch.md").read_text(encoding="utf-8") == "n/a"
    assert "missing key: metrics.nope.x" in capsys.readouterr().err


def test_thesis_placeholders_resolve_against_real_metrics(tmp_path, monkeypatch):
    """Все плейсхолдеры глав записки есть в reports/metrics.json и data/manifest.json."""
    root = SCRIPT.parents[1]
    if not (root / "reports" / "metrics.json").exists():
        pytest.skip("reports/metrics.json is produced by `geoqc eval gates`")
    chapters = sorted(str(p) for p in (root / "docs" / "thesis").glob("*.md"))
    monkeypatch.chdir(root)
    assert _run(monkeypatch, "fill", *chapters, "--out-dir", str(tmp_path)) == 0
