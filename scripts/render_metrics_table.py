#!/usr/bin/env python
"""Render Markdown tables from reports/metrics.json and fill {{metrics.*}} / {{manifest.*}} placeholders.

Usage:
  python scripts/render_metrics_table.py table [--metrics reports/metrics.json]
  python scripts/render_metrics_table.py fill docs/thesis/*.md [--metrics ...] [--manifest data/manifest.json] [--out-dir build/thesis]
Placeholders: {{metrics.detection.test.f1}}, {{metrics.gates.G3.actual}}, {{manifest.image.acquired}}.
Missing keys are rendered as "n/a" and listed on stderr; the script never invents values.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PLACEHOLDER = re.compile(r"\{\{(metrics|manifest)\.([A-Za-z0-9_.@]+)\}\}")


def get_path(obj: Any, path: str) -> Any:
    for key in path.split("."):
        if isinstance(obj, dict) and key in obj:
            obj = obj[key]
        else:
            return None
    return obj


def fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, list):
        return ", ".join(fmt(v) for v in value)
    return str(value)


def gates_table(m: dict[str, Any]) -> str:
    rows = ["| Гейт | Метрика | Цель | Факт | Пройден | Примечание |", "|---|---|---|---|---|---|"]
    for name, g in sorted((m.get("gates") or {}).items()):
        rows.append(
            f"| {name} | {g.get('metric', '')} | {g.get('op', '')} {fmt(g.get('target'))} "
            f"| {fmt(g.get('actual'))} | {fmt(g.get('passed'))} | {g.get('note', '')} |"
        )
    return "\n".join(rows)


def detection_table(m: dict[str, Any]) -> str:
    rows = [
        "| Сплит | TP | FP | FN | Precision | Recall | F1 | mean IoU |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for split in ("val", "test"):
        d = get_path(m, f"detection.{split}") or {}
        rows.append(
            f"| {split} | {fmt(d.get('tp'))} | {fmt(d.get('fp'))} | {fmt(d.get('fn'))} | "
            f"{fmt(d.get('precision'))} | {fmt(d.get('recall'))} | {fmt(d.get('f1'))} | "
            f"{fmt(d.get('mean_iou_matched'))} |"
        )
    return "\n".join(rows)


def fill(text: str, sources: dict[str, dict[str, Any]], missing: set[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        src, path = match.group(1), match.group(2)
        value = get_path(sources.get(src) or {}, path)
        if value is None:
            missing.add(f"{src}.{path}")
        return fmt(value)

    return PLACEHOLDER.sub(repl, text)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["table", "fill"])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--metrics", type=Path, default=Path("reports/metrics.json"))
    ap.add_argument("--manifest", type=Path, default=Path("data/manifest.json"))
    ap.add_argument("--out-dir", type=Path, default=Path("build/docs"))
    args = ap.parse_args()
    m = load_json(args.metrics)
    if args.mode == "table":
        print(
            f"Источник: `{args.metrics}`, commit `{m.get('git_commit', 'n/a')}`, {m.get('generated_at', 'n/a')}\n"
        )
        print(detection_table(m), "\n")
        print(gates_table(m))
        return 0
    sources = {"metrics": m, "manifest": load_json(args.manifest)}
    missing: set[str] = set()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for src in args.files:
        out = fill(src.read_text(encoding="utf-8"), sources, missing)
        (args.out_dir / src.name).write_text(out, encoding="utf-8")
    for path in sorted(missing):
        print(f"missing key: {path}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
