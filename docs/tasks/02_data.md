# ТЗ T2 — Data Agent: AOI, снимок, вектор, тайлы, PostGIS

**Цель.** `make data` (= `geoqc data all`) с нуля получает снимок и вектор выбранной AOI, приводит их к контракту и загружает в PostGIS. Работа с **реальными данными** — обязательна.

**Шаги.**
1. `search-aoi`: запросить OpenAerialMap API (`https://api.openaerialmap.org/meta?bbox=...&gsd_to=0.5`) — сначала bbox РФ, затем глобально. Критерии кандидата: GSD ≤ 0.5 м, площадь снимка ≥ 5 км², лицензия CC-BY/CC0, RGB, дата известна; плотность зданий OSM в bbox (Overpass `count`) ≥ 300 на 5 км²; смешанная застройка (оценить по тегам `building=apartments|house|industrial`). Fallback — Maxar Open Data (STAC `https://maxar-opendata.s3.amazonaws.com/events/catalog.json`, pre-event снимки, CC BY-NC 4.0). Ранжированный список → `reports/aoi_candidates.json` с полями и причинами отсева. Выбрать лучший, вырезать AOI 5–10 км² (прямоугольник или полигон) → `config/aoi.geojson` (EPSG:4326, properties: name, source, image_id, utm_epsg). Дописать в `LOG.md` и предложить текст D-001a (координатор перенесёт в DECISIONS).
2. `download`: скачать исходник в `data/raw/`, sha256, манифест (§3.4). Для COG-источников допускается чтение по HTTP с обрезкой окна (rasterio + `/vsicurl/`), тогда в raw сохраняется вырезка + URL.
3. `preprocess`: перепроецирование в UTM (`geoqc.data.crs.utm_epsg_for`), обрезка по AOI, RGB uint8, COG (`rio_cogeo.cog_translate`), → `data/interim/aoi_cog.tif`. Обновить `config/pipeline.yaml → crs.metric` через безопасную правку одного ключа (ruamel/yaml с сохранением остального) — это единственное разрешённое исключение по записи.
4. `tile`: 512×512, overlap 64, nodata заполнение, `tiles.gpkg` (§3.3). Проверка: покрытие AOI полное, число тайлов записано в LOG.
5. `osm`: Overpass (`[out:json][timeout:180]; (way["building"](bbox); relation["building"](bbox);); out geom;`) с ретраями и кэшем ответа в `data/raw/osm_<date>.json`; полигонизация way/relation (multipolygon) → `data/interim/osm_buildings.geojson` (`osm_id, tags, snapshot_date`).
6. `secondary`: Microsoft GlobalMLBuildingFootprints (dataset-links.csv по quadkey, фильтр по bbox) или Google Open Buildings (если регион покрыт) → `data/interim/secondary_buildings.geojson`. Если недоступно — честно записать `secondary.source = null`.
7. `load-ref`: загрузка `osm` (source='osm') и `secondary` в `buildings_ref` (MultiPolygon, валидизация `make_valid`, дедупликация по osm_id), GiST-индекс уже в схеме. Идемпотентно: перед загрузкой `DELETE WHERE source=...`.
8. `check-georef`: автоматический подбор ≥ 10 контрольных объектов (крупные изолированные здания OSM), оценка смещения между центроидом OSM-полигона и центроидом яркостного/градиентного отклика на снимке (или фазовая корреляция окна с растеризованной маской OSM); медиана → `reports/georef.json`, `manifest.georef`. Если медиана > 2 м — вычислить сдвиг и применить в `preprocess --shift`. Метод и его ограничения описать в README.
9. `all` — последовательность 2–8 с пропуском уже готовых артефактов (`--force` для пересборки).

**Тесты `tests/data/`:** `utm_epsg_for` (несколько полушарий), тайлинг на синтетическом растре (число тайлов, перекрытие, размер, индекс), полигонизация OSM way/relation с дыркой, идемпотентность `load-ref` (маркер `db`), парсинг ответа OAM (фикстура json). Сетевые тесты — маркер `network`.

**DoD.** `geoqc data all` реально выполнен, все артефакты на месте, `manifest.json` полный, `reports/georef.json` есть, `buildings_ref` заполнена (COUNT в LOG), G1 фактическое значение в LOG. README модуля описывает выбор AOI, лицензии, атрибуцию и как подменить снимок на свой GeoTIFF (`geoqc data preprocess --input path`).

**Запрещено.** Тайлы Esri/Bing/Google/Яндекс как источник растров. Чтение `data/labels/test/`.
