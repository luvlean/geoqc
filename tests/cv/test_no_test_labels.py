"""Статическая проверка: код CV и ноутбуки не ссылаются на каталог тестовой разметки."""

from __future__ import annotations

from pathlib import Path

FORBIDDEN = "labels/" + "test"  # built at runtime so this file itself does not contain the literal


# First pass over the test labels (D-022): these two scripts edit them on purpose and never train.
TEST_LABELLING = {"refine_drafts.py", "apply_test_review.py"}


def test_cv_sources_and_notebooks_never_mention_test_labels(repo_root: Path):
    """grep 'labels/test' по src/geoqc/cv, notebooks/ и скриптам обучения/сравнения моделей пуст."""
    offenders = []
    for base in (
        repo_root / "src" / "geoqc" / "cv",
        repo_root / "notebooks",
        repo_root / "scripts" / "experiments",
    ):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if (
                p.is_file()
                and p.name not in TEST_LABELLING
                and p.suffix in {".py", ".ipynb", ".md", ".yaml", ".yml", ".txt"}
                and FORBIDDEN in p.read_text(encoding="utf-8", errors="ignore")
            ):
                offenders.append(str(p))
    assert offenders == []
