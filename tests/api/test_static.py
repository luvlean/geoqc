"""Статика web/: файлы отдаются с Cache-Control: no-cache, чтобы правки не застревали в кэше браузера."""

from __future__ import annotations

from fastapi.testclient import TestClient

from geoqc.api.main import create_app
from geoqc.db import get_dsn


def test_static_files_revalidate(tmp_path):
    """Страница и app.js приходят с no-cache, повторный запрос app.js по ETag даёт 304."""
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="/style.css"><script src="/app.js"></script>'
        '<script src="https://unpkg.com/x.js"></script>',
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text("console.log(1);", encoding="utf-8")
    app = create_app(dsn=get_dsn(test=True), arcgis_api_key="", paths={"web": tmp_path})
    with TestClient(app) as c:
        page = c.get("/")
        script = c.get("/app.js")
        again = c.get("/app.js", headers={"If-None-Match": script.headers["etag"]})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert script.headers["cache-control"] == "no-cache"
    assert again.status_code == 304


def test_index_versions_local_assets(tmp_path):
    """index.html получает ?v=<mtime> у локальных js/css (Safari держит старый CSS), CDN не трогается."""
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="/style.css"><script src="/app.js"></script>'
        '<script src="https://unpkg.com/x.js"></script>',
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text("console.log(1);", encoding="utf-8")
    app = create_app(dsn=get_dsn(test=True), arcgis_api_key="", paths={"web": tmp_path})
    with TestClient(app) as c:
        html = c.get("/").text
        assert c.get("/index.html").text == html
    version = max(int(f.stat().st_mtime) for f in tmp_path.iterdir())
    assert f'href="/style.css?v={version}"' in html
    assert f'src="/app.js?v={version}"' in html
    assert 'src="https://unpkg.com/x.js"' in html
