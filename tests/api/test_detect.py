"""POST /detect: загрузка снимка, детекция тестовым предиктором, ошибки формата; выключение конфигом."""

from __future__ import annotations

import io

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from geoqc.api.deps import load_api_config
from geoqc.api.main import create_app
from geoqc.cv.predictor import MockPredictor
from geoqc.db import get_dsn


def _png() -> bytes:
    img = np.full((600, 700, 3), 40, np.uint8)
    img[100:160, 400:560] = 255
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue()


def _client(**detect) -> TestClient:
    cfg = load_api_config()
    cfg["detect"].update(detect)
    app = create_app(dsn=get_dsn(test=True), arcgis_api_key="", config=cfg, serve_static=False)
    app.state.ctx.detector = MockPredictor(threshold=200)
    return TestClient(app)


def test_detect_png_returns_outlines_in_pixels():
    """PNG без привязки: одно здание 864 м², контуры в пикселях, превью JPEG, гео-слоя нет."""
    with _client() as c:
        assert c.get("/config/map").json()["detect_tools"] is True
        assert c.get("/detect/config").json()["target_gsd_m"] == 0.3
        r = c.post(
            "/detect", files={"file": ("roof.png", _png(), "image/png")}, data={"conf": "0.5"}
        )
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["width"], j["height"], j["gsd_from"], j["georeferenced"]) == (
        700,
        600,
        "default",
        False,
    )
    feats = j["features"]["features"]
    assert len(feats) == 1 and abs(feats[0]["properties"]["area_m2"] - 864) < 20
    assert j["features_wgs84"] is None and j["image"].startswith("data:image/jpeg;base64,")


def test_detect_rejects_bad_input():
    """Не растр — 422 с понятным текстом; пустой файл — 422; больше лимита — 413."""
    with _client(max_upload_mb=0) as c:
        assert c.post("/detect", files={"file": ("a.png", _png())}).status_code == 413
    with _client() as c:
        bad = c.post("/detect", files={"file": ("notes.txt", b"hello")})
        assert bad.status_code == 422 and "формат" in bad.json()["detail"]
        assert c.post("/detect", files={"file": ("e.png", b"")}).status_code == 422


def test_detect_can_be_disabled():
    """detect.enabled: false — эндпоинта нет, веб не показывает вкладку."""
    with _client(enabled=False) as c:
        assert c.get("/config/map").json()["detect_tools"] is False
        assert c.post("/detect", files={"file": ("a.png", _png())}).status_code in (404, 405)
