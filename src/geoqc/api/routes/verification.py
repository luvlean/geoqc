"""Manual verification from the web map: ``reports/verification.csv`` read and written in place.

The sample itself comes from ``geoqc eval verification-sample``; the API only fills ``verdict`` and
``comment`` of rows already in it, so ``geoqc eval discrepancies`` reads the file unchanged.
"""

from __future__ import annotations

import csv
import os
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from geoqc.api.deps import VERDICTS, AppContext, get_ctx

router = APIRouter(tags=["verification"])

_lock = threading.Lock()


class VerdictUpdate(BaseModel):
    verdict: str | None = Field(None, description=f"One of {', '.join(VERDICTS)}; null clears it.")
    comment: str | None = Field(None, max_length=500)


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    missing = {"id", "class", "verdict"} - set(fields)
    if missing:
        raise HTTPException(status_code=500, detail=f"{path.name} lacks columns {sorted(missing)}")
    return fields, rows


def _source(ctx: AppContext) -> Path:
    """The filled file when it exists, otherwise the untouched sample."""
    target, sample = ctx.paths["verification"], ctx.paths["verification_sample"]
    if target.exists():
        return target
    if sample.exists():
        return sample
    raise HTTPException(
        status_code=404,
        detail="no verification sample: run `geoqc eval verification-sample` first",
    )


def _write_atomic(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    # Rewrite through a temp file so a crash never leaves a half-written CSV.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _item(n: int, row: dict[str, str]) -> dict[str, Any]:
    def num(key: str) -> float | None:
        value = (row.get(key) or "").strip()
        return float(value) if value else None

    return {
        "n": n,
        "id": int(row["id"]),
        "class": row["class"],
        "confidence": num("confidence"),
        "iou": num("iou"),
        "verdict": (row.get("verdict") or "").strip() or None,
        "comment": (row.get("comment") or "").strip() or None,
    }


def _payload(ctx: AppContext, source: Path, rows: list[dict[str, str]]) -> dict[str, Any]:
    items = [_item(i + 1, r) for i, r in enumerate(rows)]
    return {
        "path": str(ctx.paths["verification"]),
        "source": source.name,
        "verdicts": list(VERDICTS),
        "total": len(items),
        "done": sum(1 for it in items if it["verdict"]),
        "items": items,
    }


@router.get("/verification", summary="Verification sample with current verdicts")
def get_verification(ctx: Annotated[AppContext, Depends(get_ctx)]) -> dict[str, Any]:
    source = _source(ctx)
    _, rows = _read(source)
    return _payload(ctx, source, rows)


@router.patch("/verification/{disc_id}", summary="Set the verdict of one sampled discrepancy")
def patch_verification(
    disc_id: int, body: VerdictUpdate, ctx: Annotated[AppContext, Depends(get_ctx)]
) -> dict[str, Any]:
    verdict = (body.verdict or "").strip().lower()
    if verdict and verdict not in VERDICTS:
        raise HTTPException(
            status_code=422, detail=f"verdict must be one of {list(VERDICTS)} or null"
        )
    target = ctx.paths["verification"]
    with _lock:
        # Until the first verdict rows come from the sample; it is never written to.
        fields, rows = _read(_source(ctx))
        matched = [r for r in rows if r["id"].strip() == str(disc_id)]
        if not matched:
            raise HTTPException(
                status_code=404, detail=f"discrepancy {disc_id} is not in the verification sample"
            )
        for row in matched:
            row["verdict"] = verdict
            if body.comment is not None and "comment" in fields:
                row["comment"] = body.comment.strip()
        _write_atomic(target, fields, rows)
    return _payload(ctx, target, rows)
