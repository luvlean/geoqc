"""Режим тестировщика: GET/PATCH /verification пишут вердикты в reports/verification.csv, по умолчанию выключен."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from geoqc.api.deps import REVIEW_ENV
from geoqc.api.main import create_app
from geoqc.db import get_dsn

HEADER = "id,class,confidence,iou,lon,lat,verdict,comment\n"
ROWS = (
    "2673,geometry_mismatch,0.820000,0.290082,30.439938,59.710032,,\n"
    "3688,missing_in_map,0.611000,,30.427931,59.712985,,\n"
)


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    sample = tmp_path / "verification_sample.csv"
    sample.write_text(HEADER + ROWS, encoding="utf-8")
    return {"verification": tmp_path / "verification.csv", "verification_sample": sample}


@pytest.fixture
def client(files: dict[str, Path]) -> Iterator[TestClient]:
    app = create_app(
        dsn=get_dsn(test=True), arcgis_api_key="", serve_static=False, paths=files, review=True
    )
    with TestClient(app) as c:
        yield c


def test_get_reads_sample_until_first_verdict(client, files):
    """До первого вердикта отдаётся выборка, verification.csv не создаётся."""
    body = client.get("/verification").json()
    assert body["source"] == "verification_sample.csv"
    assert (body["total"], body["done"]) == (2, 0)
    assert [(it["n"], it["id"], it["verdict"]) for it in body["items"]] == [
        (1, 2673, None),
        (2, 3688, None),
    ]
    assert body["items"][1]["iou"] is None
    assert not files["verification"].exists()


def test_patch_writes_only_verdict_and_comment(client, files):
    """Вердикт пишется в verification.csv, остальные значения и выборка не меняются."""
    resp = client.patch("/verification/3688", json={"verdict": "Map_Error", "comment": "снесено"})
    assert resp.status_code == 200
    assert (resp.json()["done"], resp.json()["source"]) == (1, "verification.csv")
    assert files["verification"].read_text(encoding="utf-8") == (
        HEADER
        + "2673,geometry_mismatch,0.820000,0.290082,30.439938,59.710032,,\n"
        + "3688,missing_in_map,0.611000,,30.427931,59.712985,map_error,снесено\n"
    )
    assert files["verification_sample"].read_text(encoding="utf-8") == HEADER + ROWS


def test_patch_null_clears_verdict_and_keeps_comment(client, files):
    """verdict=null снимает вердикт; комментарий без поля comment в запросе сохраняется."""
    client.patch("/verification/2673", json={"verdict": "unsure", "comment": "тень"})
    body = client.patch("/verification/2673", json={"verdict": None}).json()
    item = body["items"][0]
    assert (item["verdict"], item["comment"], body["done"]) == (None, "тень", 0)


def test_patch_rejects_unknown_verdict_and_foreign_id(client, files):
    """Недопустимый вердикт — 422, id вне выборки — 404; файл не создаётся."""
    assert client.patch("/verification/2673", json={"verdict": "maybe"}).status_code == 422
    assert client.patch("/verification/1", json={"verdict": "unsure"}).status_code == 404
    assert not files["verification"].exists()


def test_get_without_sample_is_404(tmp_path):
    """Без выборки API подсказывает запустить verification-sample."""
    paths = {
        "verification": tmp_path / "verification.csv",
        "verification_sample": tmp_path / "verification_sample.csv",
    }
    app = create_app(
        dsn=get_dsn(test=True), arcgis_api_key="", serve_static=False, paths=paths, review=True
    )
    with TestClient(app) as c:
        resp = c.get("/verification")
    assert resp.status_code == 404
    assert "verification-sample" in resp.json()["detail"]


def test_review_tools_off_by_default(files, monkeypatch):
    """Без флага маршрутов /verification нет, а /config/map сообщает review_tools=false."""
    monkeypatch.delenv(REVIEW_ENV, raising=False)
    app = create_app(dsn=get_dsn(test=True), arcgis_api_key="", serve_static=False, paths=files)
    with TestClient(app) as c:
        assert c.get("/verification").status_code == 404
        assert c.patch("/verification/2673", json={"verdict": "unsure"}).status_code == 404
        assert c.get("/config/map").json()["review_tools"] is False
    assert not files["verification"].exists()


def test_review_tools_on_by_env(files, monkeypatch):
    """GEOQC_API_REVIEW=1 (geoqc api serve --review) включает маршруты и флаг в /config/map."""
    monkeypatch.setenv(REVIEW_ENV, "1")
    app = create_app(dsn=get_dsn(test=True), arcgis_api_key="", serve_static=False, paths=files)
    with TestClient(app) as c:
        assert c.get("/verification").status_code == 200
        assert c.get("/config/map").json()["review_tools"] is True
