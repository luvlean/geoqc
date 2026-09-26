# ТЗ T7 — App Agent: FastAPI, Leaflet, Docker

**Цель.** `make demo` поднимает сервис: карта с подложкой-снимком, слоями OSM/детекции/расхождения, карточка объекта, принять/отклонить, экспорт очереди.

**Выходы.**
- `src/geoqc/api/`: `main.py` (FastAPI, lifespan с пулом `psycopg_pool.ConnectionPool`, CORS, статика `web/` по `/`), `routes/discrepancies.py` (§8: list с фильтрами и валидацией bbox, get, patch статуса с `updated_at`, export), `routes/tiles.py` (`/tiles/{z}/{x}/{y}.mvt` через `ST_AsMVT` с `ST_TileEnvelope`, три слоя, `Cache-Control`), `routes/imagery.py` (`/imagery/{z}/{x}/{y}.png` из `data/interim/aoi_cog.tif` через rio-tiler `Reader.tile`, прозрачный PNG вне покрытия, 404 если COG нет), `routes/meta.py` (`/health`, `/stats`, `/config/map`: центр/bbox из `config/aoi.geojson`, атрибуции из `data/manifest.json` (OSM ODbL, источник снимка), `esri_enabled = bool(ARCGIS_API_KEY)`, ключ на клиент не отдавать напрямую — проксировать `/basemap/esri/{z}/{y}/{x}` с подстановкой токена на сервере), `schemas.py` (pydantic), `cli.py` (`serve --host --port --reload`).
- `web/index.html`, `web/app.js`, `web/style.css` (Leaflet 1.9 с CDN + `leaflet.vectorgrid` для MVT либо GeoJSON-слой с перезапросом по bbox при `moveend` — выбрать один путь, второй не тянуть): подложки OSM Standard / снимок (`/imagery`) / Esri (если enabled); слои `buildings_ref`, `buildings_det`, `discrepancies` цветом по классу; панель фильтров (класс, min_conf, статус); список топ-N по confidence с прокруткой к объекту; карточка: класс, confidence, IoU, osm_id (ссылка на osm.org), кнопки «Принять» / «Отклонить» → PATCH; кнопка «Экспорт GeoJSON»; атрибуция всех источников в углу карты. Без сборщиков и npm.
- `config/api.yaml`: host, port, default_limit, max_limit, mvt layers, cache seconds.
- `tests/api/`: `httpx.AsyncClient`/`TestClient` с маркером `db` — вставка синтетических расхождений в тестовую БД → list с bbox/class/min_conf, сортировка по confidence, patch статуса (валидный/невалидный), export формат §3.8, `/health`, `/config/map` без ключа → `esri_enabled=false`; без БД — валидация параметров (некорректный bbox → 422), pydantic-схемы.

**Производительность.** Для AOI 10 км² `GET /discrepancies` с bbox всей AOI < 1 с при 5 000 записей — проверить `EXPLAIN` на индексы из `001_schema.sql`, записать время в LOG.

**DoD.** `geoqc api serve` работает локально на тестовых данных; при наличии реальных `discrepancies` — скриншот не нужен, но сценарий «фильтр → карточка → статус → экспорт» пройден вручную через curl и описан в README модуля с примерами запросов.
