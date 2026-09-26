# geoqc.api — FastAPI-сервис и веб-карта очереди проверки

Модуль T7 (App Agent). Отдаёт очередь расхождений из PostGIS как GeoJSON и MVT, тайлы снимка из COG,
статику Leaflet-карты (`web/`) и прокси подложки Esri без передачи ключа клиенту (DECISIONS D-002).
Контракт API — `docs/CONTRACTS.md §8`, формат экспорта — §3.8, схема БД — `sql/001_schema.sql`
(+ вспомогательные таблицы `sql/002_matching.sql`, D-012).

## Запуск

```bash
.venv/bin/geoqc api serve --host 0.0.0.0 --port 8000 [--reload]   # ASGI: geoqc.api.main:app
# карта: http://localhost:8000/   Swagger: http://localhost:8000/docs
```

Настройки: `config/api.yaml` (см. ниже), секреты — `.env` (`DATABASE_URL`, `ARCGIS_API_KEY`).
Сервис стартует даже без данных: нет `config/aoi.geojson`/`data/manifest.json` → `/config/map` отдаёт
`null`-центр и поле `note`; нет `data/interim/aoi_cog.tif` → `/imagery/...` → 404, подложка «снимок»
не показывается; недоступна БД → `/health` = `{"status":"degraded","db":false}`, остальные ручки → 503.

## Эндпоинты

| Метод и путь | Назначение |
|---|---|
| `GET /health` | `{"status":"ok","db":true}` |
| `GET /discrepancies?bbox=&class=&min_conf=&status=&limit=&offset=` | FeatureCollection, `confidence DESC`; `numberReturned`, `numberMatched`; `ref_osm_id` из `buildings_ref` |
| `GET /discrepancies/{id}` | Feature; при наличии `discrepancy_features` — `properties.features` (признаки скоринга, D-012) |
| `PATCH /discrepancies/{id}` body `{"status":"new|confirmed|rejected"}` | меняет `status`, `updated_at = now()`, возвращает Feature |
| `GET /export?class=&min_conf=&status=[&bbox=]` | вся отфильтрованная очередь, properties ровно §3.8, `Content-Disposition: attachment` (MapRoulette/JOSM) |
| `GET /tiles/{z}/{x}/{y}.mvt[?layers=a,b]` | MVT: слои `discrepancies`, `buildings_ref`, `buildings_det` (`ST_AsMVT` + `ST_TileEnvelope`), `Cache-Control`; подавленные NMS детекции (`det_suppressed`) исключены |
| `GET /imagery/{z}/{x}/{y}.png` | тайл COG через rio-tiler `Reader.tile`; прозрачный PNG вне покрытия; 404 без COG |
| `GET /stats` | счётчики по классам/статусам, `model_versions`, `buildings: {ref, det, det_suppressed}` |
| `GET /config/map` | центр/bbox AOI, атрибуции (OSM ODbL, источник снимка, Esri), подложки, `esri_enabled`, `note` |
| `GET /basemap/esri/{z}/{y}/{x}` | прокси Esri World Imagery, токен подставляется на сервере; 404 без `ARCGIS_API_KEY` |
| `GET /` | статика `web/` (index.html, app.js, style.css) |

Валидация: `bbox` — 4 числа, `minx<maxx`, `miny<maxy`, в пределах WGS84; `class`/`status` — списки через
запятую из допустимых значений; `min_conf ∈ [0,1]`; `1 ≤ limit ≤ max_limit`; `offset ≥ 0`; ошибки → 422.

## Сценарий «фильтр → карточка → статус → экспорт» (curl)

```bash
API=http://localhost:8010
curl -s $API/health
curl -s $API/config/map | jq '{aoi, esri_enabled, imagery_available, note}'

# 1. фильтр: bbox AOI, два класса, порог уверенности, только новые, топ-3
curl -s "$API/discrepancies?bbox=30.30,59.93,30.36,59.96&class=missing_in_map,geometry_mismatch&min_conf=0.9&status=new&limit=3" \
  | jq '{numberReturned, numberMatched, ids: [.features[].id]}'

# 2. карточка объекта
ID=$(curl -s "$API/discrepancies?bbox=30.30,59.93,30.36,59.96&limit=1" | jq '.features[0].id')
curl -s $API/discrepancies/$ID | jq '.properties | {class, confidence, iou, status, ref_osm_id, features}'

# 3. статус (принять / отклонить / вернуть в new); невалидный статус -> 422
curl -s -X PATCH -H 'Content-Type: application/json' -d '{"status":"confirmed"}' $API/discrepancies/$ID | jq '.properties.status'
curl -s -o /dev/null -w '%{http_code}\n' -X PATCH -H 'Content-Type: application/json' -d '{"status":"maybe"}' $API/discrepancies/$ID

# 4. экспорт подтверждённых с уверенностью >= 0.5 (файл для MapRoulette / JOSM)
curl -s -o discrepancies.geojson -D - "$API/export?status=confirmed&min_conf=0.5" | grep -i 'content-'
jq '.features[0].properties | keys' discrepancies.geojson   # class confidence created_at id iou model_version ref_osm_id status

# тайлы и снимок
curl -s -o tile.mvt -D - "$API/tiles/14/9572/4762.mvt" | grep -i 'content-type\|cache-control'
curl -s -o /dev/null -w '%{http_code}\n' "$API/imagery/14/9572/4762.png"   # 404, пока нет data/interim/aoi_cog.tif
```

Прогон 2026-09-26 на 5 000 синтетических строк (тестовая БД, `geoqc api bench --keep`, сервер на :8010):
все шаги вернули ожидаемые коды (200/422/404), PATCH обновил `updated_at`, экспорт дал 489 объектов
с ровно восемью properties §3.8.

## Веб-карта (`web/`)

Leaflet 1.9.4 + Leaflet.VectorGrid 1.3.0 с CDN (unpkg), без сборщиков. Выбран **путь MVT**: один
VectorGrid-слой читает `/tiles/{z}/{x}/{y}.mvt` и рисует все три слоя (OSM — синий контур, детекции —
зелёный пунктир, расхождения — заливка цветом класса; `confirmed` жирнее, `rejected` пунктир). JSON-ручка
`/discrepancies?bbox=` используется только для списка топ-N (20/50/100/200) по видимой области и карточки.
Фильтры (класс, `min_conf`, статус) применяются к тайлам на клиенте через функцию стиля и к списку/экспорту
через параметры запроса. Клик по объекту на карте или в списке → карточка (класс, confidence, IoU, статус,
`osm_id` со ссылкой на osm.org, признаки скоринга) → «Принять»/«Отклонить»/«Сбросить» → `PATCH`, стиль
объекта обновляется через `setFeatureStyle`. «Экспорт GeoJSON» скачивает `/export` с текущими фильтрами.
Подложки: OSM Standard, «Снимок (COG)» (если есть), Esri World Imagery (если `esri_enabled`). Атрибуция
OSM (ODbL) и источника снимка добавлена в угол карты постоянно (данные слоёв производны от них), Esri —
при включённой подложке; юридическая сноска в подвале панели. Если CDN VectorGrid недоступен — слои MVT
отключаются с предупреждением, список и карточка продолжают работать.

## `config/api.yaml`

| Ключ | Значение по умолчанию | Смысл |
|---|---|---|
| `server.host/port/cors_origins` | `0.0.0.0`, `8000`, `["*"]` | адрес по умолчанию для `serve`, CORS |
| `pagination.default_limit/max_limit` | 500 / 5000 | `limit` по умолчанию и максимум (`> max` → 422) |
| `pool.min_size/max_size/timeout_s` | 1 / 8 / 5 | `psycopg_pool.ConnectionPool` |
| `cache_seconds.tiles/imagery/basemap` | 60 / 86400 / 3600 | `Cache-Control: public, max-age=` |
| `mvt.extent/buffer/max_zoom` | 4096 / 64 / 22 | параметры `ST_AsMVTGeom`, верхний зум |
| `mvt.layers.<name>.{table,min_zoom}` | discrepancies 8, buildings_* 13 | слой не отдаётся ниже `min_zoom` |
| `imagery.tile_size/resampling` | 256 / bilinear | rio-tiler |
| `paths.aoi/manifest/cog/web` | `config/aoi.geojson`, `data/manifest.json`, `data/interim/aoi_cog.tif`, `web` | относительно корня репозитория |
| `basemaps.osm.{url,attribution,max_zoom}` | tile.openstreetmap.org | подложка OSM |
| `basemaps.esri.{upstream,attribution,max_zoom,timeout_s}` | ibasemaps-api.arcgis.com | upstream прокси; ключ только из `.env` |

Файл читается напрямую (`geoqc.api.deps.load_api_config`), а не через `load_config()`, чтобы ошибка в
yaml другого модуля не мешала старту API; отсутствующие ключи берутся из `DEFAULT_CONFIG`.

## Производительность

`geoqc api bench --n 5000 --repeats 10` (вставляет синтетику в **тестовую** БД, печатает `EXPLAIN ANALYZE`
и латентности через `TestClient`, затем очищает таблицы; `--keep` оставляет строки для ручной проверки).
Замер 2026-09-26, macOS, PostGIS 3.6.4 в Docker, 5 000 расхождений на bbox ~11 км²:

| Запрос | медиана | максимум |
|---|---|---|
| `GET /discrepancies?bbox=<AOI>` (limit 500, 241 KB) | 21 ms | 112 ms |
| `GET /discrepancies?bbox=<AOI>&limit=5000` (2.4 MB) | 139 ms | 178 ms |
| `GET /discrepancies?bbox=<AOI>&class=…&min_conf=0.5` | 14 ms | 28 ms |
| `GET /export` (все 5 000) | 175 ms | 245 ms |
| `GET /tiles/14/9572/4762.mvt` (три слоя) | 38 ms | 176 ms |
| `GET /stats` | 5 ms | 11 ms |

Через uvicorn с curl: `/discrepancies?bbox=<AOI>` 89 ms, `limit=5000` 159 ms — требование «< 1 с на AOI» выполнено.
`EXPLAIN ANALYZE` bbox-запроса: на bbox всей AOI планировщик берёт `Seq Scan` (все 5 000 строк подходят,
152 буфера, 14 ms) + `top-N heapsort` по `confidence DESC`; на bbox 1/100 AOI — `Bitmap Index Scan on
discrepancies_geom_gist` (2.5 ms). Индексы `001_schema.sql` (`geom` GiST, `(class, confidence DESC)`,
`status`) достаточны; дополнительные не нужны. Время в основном уходит на `ST_AsGeoJSON` и сериализацию.

## Тесты

`pytest tests/api -q` — 69 тестов: без БД — валидация параметров, pydantic-схемы, `/config/map` без файлов и
с синтетическими `aoi.geojson`/`manifest.json`, прокси Esri с подменённым `requests.get` (ключ не утекает),
тайлы снимка из синтетического GeoTIFF (UTM 36N), CLI; с маркером `db` — вставка синтетических строк через
`db_conn` → список (bbox/class/min_conf/status/сортировка/пагинация), карточка, PATCH (валидный/невалидный/404),
экспорт §3.8, `/stats`, MVT (слои, `min_zoom`, `layers=`, пустой тайл, исключение `det_suppressed`).

## Зависимости и ограничения

- Модули: читает таблицы Geo (`discrepancies`, `matches` не используется, `det_suppressed`,
  `discrepancy_features`), Data (`config/aoi.geojson`, `data/manifest.json`, `data/interim/aoi_cog.tif`),
  CV (`buildings_det`). Ничего не пишет, кроме `discrepancies.status/updated_at`.
- Пакеты: fastapi, uvicorn, pydantic 2, psycopg[pool], rio-tiler, rasterio, shapely, pyyaml, requests
  (прокси Esri; httpx есть только в dev). Всё уже в `requirements.txt`.
- `ref_osm_id` — ссылка вида `openstreetmap.org/way/<id>`: в `buildings_ref` нет типа объекта OSM
  (way/relation), для мультиполигонов-relation ссылка будет неверной.
- MVT-тайлы кэшируются 60 с; после `PATCH` клиент обновляет стиль объекта локально
  (`setFeatureStyle`), полная перезагрузка тайлов — по кнопке «Обновить» или при смене фильтров.
- Docker-сервис `app` и `make demo` — у Orchestrator; нужен только `geoqc api serve` и
  `geoqc.api.main:app`.
