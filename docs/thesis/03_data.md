# Глава 3. Данные

Все факты о данных берутся из `data/manifest.json` (контракт §3.4) и `DECISIONS.md`; в тексте — плейсхолдеры `{{manifest.*}}`. Реализация — модули `geoqc data`, `geoqc annotation` (`src/geoqc/data/README.md`, `src/geoqc/annotation/README.md`).

## 3.1 Зона интереса

Критерии выбора (D-001): открытый снимок с GSD ≤ 0.5 м и лицензией, допускающей обработку; площадь 5–10 км²; смешанная застройка (многоэтажная, частный сектор, промзона); хорошее покрытие OSM. Кандидаты ранжированы командой `geoqc data search-aoi` (сначала покрытие OpenAerialMap по РФ, затем глобально; fallback — Maxar Open Data) и сохранены в `reports/aoi_candidates.json` с причинами отсева.

Выбранная зона: `{{manifest.aoi.name}}`, площадь `{{manifest.aoi.area_km2}}` км², bbox `{{manifest.aoi.bbox_wgs84}}`, метрическая зона EPSG:`{{manifest.aoi.utm_epsg}}`. Обоснование выбора — `DECISIONS.md` D-001a.

## 3.2 Снимок

Источник — `{{manifest.image.source}}`, идентификатор `{{manifest.image.id}}`, дата съёмки `{{manifest.image.acquired}}`, GSD `{{manifest.image.gsd_m}}` м, лицензия `{{manifest.image.license}}`, атрибуция «`{{manifest.image.attribution}}`». Исходник сохранён без изменений в `data/raw/` с контрольной суммой SHA-256 (`{{manifest.image.sha256}}`).

Препроцессинг (`geoqc data preprocess`): перепроецирование в метрическую UTM-зону, обрезка по `config/aoi.geojson`, приведение к RGB uint8, запись Cloud-Optimized GeoTIFF `data/interim/aoi_cog.tif`. Тайлинг (`geoqc data tile`): 512×512 px с перекрытием 64 px, края дополняются nodata; индекс тайлов — `data/tiles/tiles.gpkg` (`tile_id = "{row}_{col}"`). Перекрытие нужно для склейки объектов на границах тайлов (гл. 4).

Пайплайн принимает любой GeoTIFF: `geoqc data preprocess --input <файл>` — компания может подставить собственные лицензионные снимки.

## 3.3 Векторный слой

Здания OSM получены через Overpass API (запрос `way["building"]` и `relation["building"]` по bbox с геометрией), срез на дату `{{manifest.osm.snapshot_date}}`, объектов `{{manifest.osm.count}}`; ответ кэшируется в `data/raw/osm_<date>.json`. Отношения multipolygon полигонизируются с учётом внутренних колец. Слой загружается в `buildings_ref` (`source='osm'`) с валидацией геометрий (`ST_MakeValid`) и дедупликацией по `osm_id`; операция идемпотентна.

Второй источник — `{{manifest.secondary.source}}` (`{{manifest.secondary.count}}` объектов, лицензия `{{manifest.secondary.license}}`): Microsoft GlobalMLBuildingFootprints или Google Open Buildings в зависимости от покрытия региона; загружается в ту же таблицу с соответствующим `source` и используется для предразметки и признака согласия в скоринге. При отсутствии покрытия поле `secondary.source` равно `null`, и зависимые компоненты отключаются.

Разновременность фиксируется явно: дата снимка и дата среза OSM хранятся в манифесте и учитываются при интерпретации классов `missing_*`.

## 3.4 Проверка геопривязки

Смещение снимка относительно вектора оценивается на $n \ge 10$ контрольных объектах — крупных изолированных зданиях OSM — как расстояние между центроидом полигона OSM и положением здания на снимке (метод — `src/geoqc/data/README.md`; формула — `docs/metrics_and_formulas.md` §10). Медианное смещение: `{{metrics.georeference.median_offset_m}}` м по `{{metrics.georeference.n_control_points}}` точкам (`reports/georef.json`, `reports/metrics.json → georeference`). Целевое значение G1 — ≤ 2 м; при превышении применяется сдвиг, записанный в `manifest.georef.shift_applied_m`. Статус гейта: `{{metrics.gates.G1.passed}}`.

## 3.5 Эталонная разметка

OSM не может быть эталоном — именно его мы проверяем. Эталон формируется ручной разметкой по снимку (D-005):

1. **Отбор тайлов** (`geoqc annotation select-tiles`): стратифицированная выборка 20–30 тайлов по типу застройки (`dense_multistorey`, `low_rise_private`, `industrial`, `mixed`; страты — по тегам и плотности зданий OSM), детерминированный сплит `val`/`test` без пересечения тайлов (`data/labels/tiles_split.csv`).
2. **Предразметка** (`geoqc annotation pre-annotate`): черновые контуры из второго источника (или бейзлайн-детекций) обрезаются по тайлам сплита → `draft.geojson`, `verified=false`.
3. **Правка в QGIS**: студент по инструкции `docs/annotation/INSTRUCTION_QGIS.md` правит и подтверждает каждый объект (`verified=true`) согласно протоколу `docs/annotation/PROTOCOL.md` (что считать зданием, пристройки, навесы, стройки, минимальная площадь `min_area_m2`, контур по крыше с обоснованием через off-nadir, объекты на границе тайла).
4. **Слепая повторная разметка** 10 % тайлов (`recheck.geojson`) для оценки согласия (G2).
5. **Валидация** (`geoqc annotation validate --split`): валидность геометрий, все `verified=true`, площадь ≥ `min_area_m2`, отсутствие дубликатов, соответствие `tile_id` сплиту; расчёт согласия → `reports/annotation.json`.

Объём: `val` — `{{metrics.dataset.n_tiles_val}}` тайлов, `{{metrics.dataset.n_gt_val}}` зданий; `test` — `{{metrics.dataset.n_tiles_test}}` тайлов, `{{metrics.dataset.n_gt_test}}` зданий. Согласие разметки: IoU `{{metrics.annotation.agreement_iou}}`, F1 `{{metrics.annotation.agreement_f1}}` на `{{metrics.annotation.n_checked}}` проверенных объектах (G2, цель ≥ 0.80; статус `{{metrics.gates.G2.passed}}`).

Разделение: `val` — подбор порогов и дообучение; `test` — однократная финальная оценка. Каталог `data/labels/test/` недоступен модулю детекции и ноутбуку обучения; запрет проверяется статическим тестом QA (`tests/integration/test_constraints.py`).

## 3.6 Лицензии и атрибуция

| Данные | Лицензия | Использование |
|---|---|---|
| Снимок `{{manifest.image.source}}` | `{{manifest.image.license}}` | детекция, разметка, подложка |
| OSM | ODbL | проверяемый слой; атрибуция на карте и в отчёте |
| `{{manifest.secondary.source}}` | `{{manifest.secondary.license}}` | предразметка, признак согласия, сравнение |
| Esri World Imagery | условия ArcGIS; только XYZ-подложка с ключом | визуальное сравнение (D-002), не для выгрузки и обучения |
| Веса модели | по `models/MODEL_CARD.md` | детекция |

Секреты (ключ Esri, строки подключения) — только в `.env`, не в репозитории.
