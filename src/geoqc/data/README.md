# Модуль `geoqc.data` — данные: AOI, снимок, тайлы, вектор, PostGIS

## Назначение

`geoqc data all` (= `make data`) с нуля получает открытый снимок и векторные здания для выбранной
территории (AOI), приводит их к контракту `docs/CONTRACTS.md` §3.1–3.4 и загружает вектор в
PostGIS (`buildings_ref`). Все числа ниже порождены кодом и лежат в `reports/` и `data/manifest.json`.

## Выбор AOI (D-001 → предложение D-001a)

Процедура — `geoqc data search-aoi` (`aoi.py`), отчёт — `reports/aoi_candidates.json`:

1. Запрос OpenAerialMap API (`/meta?gsd_to=0.5`) — сначала грубый bbox РФ
   (`search.russia_bbox`, 27.3–180° E, 43.5–82° N; в него попадают также Финляндия, Прибалтика,
   Казахстан — флаг `in_russia_bbox` грубый и проверяется вручную по названию/провайдеру),
   затем полный глобальный список (~20 тыс. записей, постранично по 1000).
2. Фильтры по метаданным: GSD ≤ 0.5 м, площадь footprint ≥ 5 км² и ≤ 60 км² (крупнее — спутниковые
   полосы над катастрофами), лицензия CC-BY 4.0 / CC0 / CC BY-SA 4.0, дата ≥ 2015, заголовок
   без `DEM|DSM|DTM|NDVI` (у OAM много DEM-продуктов с теми же bbox).
3. Ранжирование `score()`: РФ-bbox (+100), свежесть, мелкий GSD, «удобная» площадь 5–30 км²,
   затем плотность и смешанность OSM-застройки.
4. Для top-12 — открытие заголовка по `/vsicurl/` (RGB uint8, проекция) и вычисление **связного
   валидного footprint** (маска датасета на грубом overview, erosion 30 м, упрощение 15 м):
   ортофото с дронов имеют рваные края, поэтому bbox площадь ≠ реальная. Кандидат отсеивается,
   если полигон < 5 км².
5. Для top-6 — Overpass `out count` по bbox будущего AOI: всего зданий и грубая смесь
   `apartments|residential`, `house|detached|…`, `industrial|warehouse|garages`. Порог —
   ≥ 300 зданий на 5 км².
6. Победитель → `config/aoi.geojson` (EPSG:4326; properties `name, source, image_id, utm_epsg`,
   плюс `image_url, acquired, license, attribution, native_gsd_m, area_km2`). AOI-полигон = валидный
   footprint снимка (если он > 10 км² — наибольший вписанный прямоугольник).

**Результат** (`reports/aoi_candidates.json`, D-001a). В РФ OpenAerialMap содержит серию UAV-ортофото
Санкт-Петербурга и области (провайдер Sergey Astakhov, CC-BY 4.0, 2018–2022, GSD 6–8 см).
Выбран **«Пушкин, Отдельный парк, Тярлево»** (OAM `6101142e5a90f10006fd3038`, съёмка 2021-07-26,
GSD 0.073 м, EPSG:32636, AOI 5.55 км²). В bbox 2352 здания OSM, и представлены все три страты:
многоэтажные дома, частная малоэтажная застройка Тярлево и промзона.
Ближайшие соседи по score: «Пушкин, Баболовский парк» почти целиком частный сектор,
«СПб, Невский район» почти без частных домов, поэтому страта `low_rise_private` там пуста.
Maxar Open Data (CC BY-NC 4.0) не понадобился; запасной путь описан ниже.

Лицензии и атрибуция:
- Снимок: **CC BY 4.0**, «Sergey Astakhov via OpenAerialMap». Ссылка на метаданные —
  `https://api.openaerialmap.org/meta/<oam_id>`.
- OSM: **ODbL**, «© OpenStreetMap contributors».
- Microsoft GlobalMLBuildingFootprints: **ODbL** (регион Russia, релиз в `manifest.secondary.url`).

Fallback без OAM: `search-aoi` пишет `chosen: null` и `maxar_open_data_fallback.used: true`; тогда
выбирается pre-event item из STAC `https://maxar-opendata.s3.amazonaws.com/events/catalog.json`
над хорошо размеченным городом, его `visual` COG URL прописывается в `config/data.yaml → image`
(или в `config/aoi.geojson`), AOI рисуется вручную, лицензия CC BY-NC 4.0 записывается в манифест.

## Команды

```bash
geoqc data search-aoi [--skip-global] [--choose <oam_id>] [--no-write-aoi]   # → reports/aoi_candidates.json, config/aoi.geojson
geoqc data download   [--force]              # data/raw/<source>_<id>.tif + sha256 → manifest.image
geoqc data preprocess [--input my.tif] [--shift dx,dy] [--gsd 0.3] [--force]
                                             # → data/interim/aoi_cog.tif, config/pipeline.yaml crs.metric
geoqc data tile       [--force]              # → data/tiles/{row}_{col}.tif, data/tiles/tiles.gpkg
geoqc data osm        [--force]              # Overpass → data/raw/osm_<date>.json, data/interim/osm_buildings.geojson
geoqc data secondary  [--force]              # Microsoft/Google → data/interim/secondary_buildings.geojson
geoqc data load-ref   [--source osm|secondary|all] [--test-db]   # → buildings_ref (идемпотентно)
geoqc data check-georef                      # → reports/georef.json, manifest.georef
geoqc data all [--force] [--skip-secondary] [--skip-db]
```

`all` пропускает готовые артефакты; после `check-georef`, если медиана > 2 м и систематический
вектор ≥ 0.6 м, автоматически выполняет `preprocess --shift -dx,-dy`, `tile --force` и повторный
`check-georef`.

### Свой GeoTIFF вместо OAM

```bash
geoqc data preprocess --input /path/to/ortho.tif --force && geoqc data tile --force
```
Требования: ≥ 3 канала (первые три трактуются как RGB), любая CRS с проекцией, GSD ≤ 0.5 м
после ресемплинга (`preprocess.target_gsd_m`). AOI по-прежнему берётся из `config/aoi.geojson`
(нарисуйте свой полигон, заполните `utm_epsg` или оставьте — он вычислится по центру). Манифест
получает `image.source = "user"`, `sha256` файла и путь.

## Входы / выходы

| Шаг | Вход | Выход |
|---|---|---|
| download | `config/aoi.geojson` (properties) или `data.yaml → image` | `data/raw/openaerialmap_<id>.tif`; при размере > `windowed_over_mb` — только окно AOI по `/vsicurl/` (`manifest.image.download_mode = windowed`) |
| preprocess | raw GeoTIFF | `data/interim/aoi_cog.tif` — COG deflate, RGB uint8, UTM, nodata 0, обрезка полигоном AOI, GSD `target_gsd_m` |
| tile | COG | 512×512, overlap 64, nodata 0; `tiles.gpkg` слой `tiles(tile_id,row,col,path,geom EPSG:4326)` |
| osm | bbox AOI (+50 м) | `data/raw/osm_<snapshot>.json` (сырой ответ с запросом), `osm_buildings.geojson` (`osm_id, osm_type, tags, snapshot_date`; relation → `osm_id = -id`) |
| secondary | bbox AOI | `secondary_buildings.geojson` (+ поле `secondary` с источником/URL) |
| load-ref | оба GeoJSON | `buildings_ref` (`ST_Multi(ST_CollectionExtract(ST_MakeValid(...),3))`, dedup по `osm_id`, `DELETE WHERE source=` перед вставкой) |
| check-georef | COG + osm_buildings | `reports/georef.json`, `manifest.georef` |

## Параметры `config/data.yaml`

- `aoi.*` — путь к GeoJSON, диапазон площади 5–10 км², erosion/simplify footprint.
- `search.*` — OAM API, `russia_bbox`, пороги GSD/площади/года, лицензии, regex исключения,
  `min_osm_buildings_per_5km2`, сколько кандидатов проверять по сети.
- `overpass.*` — список зеркал (порядок = приоритет; `overpass-api.de` в нашей сети часто отдаёт
  timeout, поэтому он последний), таймаут, ретраи, User-Agent.
- `image.*` — выбранный снимок (дублирует properties `aoi.geojson`, приоритет у GeoJSON),
  `raw_path`, `windowed_over_mb`.
- `preprocess.*` — `target_gsd_m` (0.3 м: 0.069 м нативных избыточны для детекции, а 512 px
  тайл = 154 м покрывает целые здания), ресемплинг (`average` вниз, `cubic` вверх), nodata, профиль COG.
- `tiles.*` — размер/перекрытие (дублируют `pipeline.yaml → tiles`, используется `pipeline.yaml`),
  `keep_only_intersecting_aoi`.
- `osm.*` — шаблон запроса, буфер bbox, `min_area_m2`.
- `secondary.*` — URL `dataset-links.csv` Microsoft, уровень quadkey (9), URL Google Open Buildings.
- `georef.*` — см. ниже.

## Проверка геопривязки (G1) — метод и ограничения

`georef.py`:
1. Контрольные здания из OSM: площадь 250–6000 м², прямоугольность (area / min_rotated_rect)
   ≥ 0.7, изолированные (нет других зданий в 12 м), `building:levels ≤ 3` если тег есть, целиком
   внутри AOI и на валидных пикселях. Порядок — детерминированный shuffle (`random_seed`), берём
   до `n_control_target` = 40, минимум `n_control_min` = 10.
2. Для каждого: окно COG вокруг здания (+2·`search_radius_m`), градиент Собеля по серому,
   растеризованный контур OSM (1 px, дилатация 1). Кросс-корреляция через FFT
   (`scipy.signal.fftconvolve`), argmax в радиусе ±10 м, субпиксельное уточнение параболой.
   Слабые пики (`peak / mean < min_peak_ratio` = 1.6) отбрасываются.
3. `median_offset_m` = медиана длин векторов смещения (это и есть G1);
   `systematic_shift_m` = (median dx, median dy) — то, что применяет `preprocess --shift`
   (с обратным знаком: снимок сдвигается к вектору). MAD остатков — мера случайного разброса.

Ограничения: (а) измеряется положение **крыши**, а OSM — контур **фундамента**: у зданий с
наклоном (off-nadir, высотные) смещение реально существует и не является ошибкой привязки, поэтому
берём только малоэтажные; (б) деревья, тени, парапеты дают ложные градиенты — отсюда порог на
пик и медиана вместо среднего; (в) модель — чистый сдвиг, поворот/масштаб не оцениваются;
(г) точность ограничена GSD COG (0.3 м) и качеством самой OSM-обводки (типичный разброс
контуров в OSM — 0.5–1.5 м), поэтому медиана ~1 м не означает ошибку снимка.

Фактические значения — в `reports/georef.json` и в разделе «Известные проблемы» ниже.

## Зависимости от других модулей

Читает: `geoqc.config` (`load_config`, `resolve_path`), `geoqc.db` (`connect`, `apply_schema`),
`config/pipeline.yaml` (tiles, image.max_gsd_m), `sql/001_schema.sql`. Пишет **один** ключ
`crs.metric` в `config/pipeline.yaml` через ruamel (комментарии сохраняются). Все остальные
модули берут CRS из `manifest.aoi.utm_epsg` / `pipeline.yaml → crs.metric`, тайлы — из
`tiles.gpkg`, вектор — из `buildings_ref`.

## Тесты

`pytest tests/data -q`: UTM-зона, тайлинг синтетического растра (число, перекрытие, nodata,
индекс), полигонизация way/relation с дыркой, фикстура OAM (`fixtures/oam_response.json` —
6 реальных записей), идемпотентность `load-ref` (маркер `db`, тестовая БД), сетевые тесты —
маркер `network` (`GEOQC_NETWORK=1`).

## Известные проблемы и что должен знать следующий агент

(см. раздел, заполняемый по итогам реального прогона ниже)
