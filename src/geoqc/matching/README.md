# geoqc.matching — сопоставление, классификация расхождений, скоринг

ТЗ — `docs/tasks/05_geo.md`. Модуль заполняет таблицы `matches` и `discrepancies`
(`sql/001_schema.sql`) по детекциям `buildings_det` и эталону `buildings_ref`, считает `confidence`
и экспортирует очередь проверки в `reports/discrepancies.geojson` (контракт §3.8).

## Логика модуля

Python управляет шагами, геометрию считает SQL (`sql/002_matching.sql`).

| Файл | Роль |
|---|---|
| `cli.py` | `init-db` (`db.apply_schema`), `run` (одна транзакция `db.connect` → `matcher.run_matching`), `export`, `stats` |
| `matcher.py` | `run_matching`: `resolve_model_version` (неизвестная версия — ошибка) → `resolve_metric_srid` → граничные случаи → назначение 1:1 (`match_greedy_sql` или `hungarian_assign` по кластерам + `write_matches`) → `classify` (SQL `geoqc_classify`) → `score_discrepancies` |
| `edge_cases.py` | обёртки над SQL: `split_multipart_detections`, `dedup_detections` (NMS → `det_suppressed`), `load_aoi_geojson` |
| `scoring.py` | чистые функции: `Features` → `confidence` по формуле класса. Веса из `config/matching.yaml` нормируются к 1; если второго источника нет, его член отбрасывается |
| `export.py` | `fetch_features` → `reports/discrepancies.geojson` с сортировкой `confidence DESC` |

Данные проходят так: `buildings_ref` + `buildings_det` нужной версии → `matches` → `discrepancies` + `discrepancy_features`. Признаки
считает SQL (`geoqc_discrepancy_features`), формулу применяет Python. Поэтому скоринг тестируется на числах без БД.

Инварианты:

- `run` идемпотентен по `model_version`: строки этой версии пересоздаются, строки других версий не трогаются;
- эталон — только `source='osm'`; `microsoft`/`google` служат лишь признаком для скоринга;
- id расхождений — BIGSERIAL, поэтому после нового `run` они другие, а статусы и вердикты старой очереди не переносятся.

Где менять: пороги и веса — `config/matching.yaml`; классификация и признаки — функции `sql/002_matching.sql`
(`CREATE OR REPLACE`, применяются через `make db-init`); формулы `confidence` — `scoring.py` и тесты на числах в
`tests/matching/test_scoring.py`.

## Входы и выходы

| Вход | Откуда |
|---|---|
| `buildings_ref` (`source='osm'` — эталон; `microsoft`/`google` — только для скоринга) | `geoqc data load-ref` |
| `buildings_det` (`model_version`, `score`) | `geoqc cv predict --to-db` |
| `config/pipeline.yaml → crs.metric` (UTM EPSG) | `geoqc data preprocess`; запасные варианты: `data/manifest.json → aoi.utm_epsg`, опция `--metric-srid`, центроид данных |
| `config/aoi.geojson` (опционально) | `geoqc data search-aoi`; без файла краевая обработка AOI выключена |
| `config/matching.yaml` | этот модуль |

| Выход | Что |
|---|---|
| `matches` | пары ref↔det с IoU ≥ `iou_match` (для G5), 1:1 |
| `discrepancies` | классы `missing_in_map`, `missing_on_image`, `geometry_mismatch`, `confidence ∈ [0,1]`, `status='new'` |
| `det_suppressed` (таблица 002) | подавленные детекции: `reason` ∈ nms, small, nested, ref_fragment; для `ref_fragment` — `ref_id` здания (сами строки `buildings_det` не удаляются) |
| `discrepancy_features` (таблица 002) | признаки за каждым `confidence` — для анализа ошибок и воспроизводимости |
| `reports/discrepancies.geojson` | FeatureCollection, EPSG:4326, properties `id, class, confidence, iou, status, ref_osm_id, model_version, created_at`, сортировка по `confidence DESC` |

## Команды CLI

```bash
geoqc matching init-db [--test-db]                 # применяет sql/001, sql/002 (идемпотентно)
geoqc matching run --model-version <v> [--algorithm greedy|hungarian] [--metric-srid 32637] [--aoi config/aoi.geojson] [--json]
geoqc matching export [--out reports/discrepancies.geojson] [--class missing_in_map,geometry_mismatch] [--min-conf 0.5] [--status new] [--limit N]
geoqc matching stats [--model-version <v>] [--json]
```

`--model-version` можно опустить, если в `buildings_det` ровно одна версия. Неизвестная версия — ошибка
(иначе все эталоны стали бы `missing_on_image`). `--test-db` переключает на `TEST_DATABASE_URL`.

Пример реального прогона (после `geoqc data load-ref` и `geoqc cv predict --to-db`):

```bash
geoqc matching init-db
geoqc matching run --model-version "$(python -c 'import yaml;print(yaml.safe_load(open("config/cv.yaml"))["model_version"])')"
geoqc matching stats
geoqc matching export
```

## Алгоритм `run` (одна транзакция, идемпотентно по `model_version`)

1. **Мультиполигоны** (`split_multipart_detections: true`) — `geoqc_split_multipart_detections`: детекция из
   нескольких частей заменяется строками по одной части (score/model_version/tile_id копируются). Эталонные
   мультиполигоны (OSM-relation) не режутся: IoU считается по всем частям.
2. **Дедупликация** (`dedup_detections: true`) — `geoqc_dedup_detections`: NMS по убыванию `score`; сосед с
   IoU > `dedup_iou` (0.7) подавляется и пишется в `det_suppressed` (`reason='nms'`); подавленные не участвуют дальше.
   **Мелкие детекции** (`suppress_small_detections: true`, D-018) — `geoqc_suppress_small_detections`: части меньше
   `min_det_area_m2` (12 м², минимум CV и разметки; осколки после разбиения его обходят) → `reason='small'`; детекция,
   которая на ≥ `nested_cover` лежит внутри другой и не больше `nested_max_area_ratio` её площади, → `reason='nested'`
   (кусок крыши поверх здания). Вложенная детекция с IoU ≥ `iou_match` к эталону остаётся: тогда большая — склейка квартала.
   **Куски зданий OSM** (`suppress_ref_fragments: true`, D-025) — `geoqc_suppress_ref_fragments`: детекция, которая на
   ≥ `ref_fragment_cover` (0,8) лежит внутри одного здания OSM и не совпадает с ним (IoU < `iou_match`), → `reason='ref_fragment'`,
   `ref_id` = это здание. Модель видела здание по частям: ни кусок, ни само здание (`missing_on_image`) не попадают в очередь.
3. **Сопоставление 1:1** — кандидаты `geoqc_candidate_pairs`: все пары (osm-ref, det) с `ST_Intersects`
   через GiST, IoU ≥ порога.
   - `greedy` (по умолчанию, SQL `geoqc_match_greedy`): пары с IoU ≥ `iou_match` по убыванию IoU (при равенстве —
     `ref_id`, `det_id`); пара принимается, если обе стороны свободны. 1:1 обеспечивают `UNIQUE (ref_id, model_version)`
     и `UNIQUE (det_id, model_version)` таблицы `matches`, поэтому цикл — `INSERT … ON CONFLICT DO NOTHING`.
   - `hungarian` (Python, `matcher.hungarian_assign`): граф пар с IoU ≥ `iou_match` разбивается на компоненты
     связности (union-find); в каждой компоненте `scipy.optimize.linear_sum_assignment` максимизирует суммарный IoU.
     Даёт больше пар на конфликтных конфигурациях (тест `test_greedy_vs_hungarian_on_conflict`).
4. **Классификация** — `geoqc_classify` (SQL) по текущему `matches`:
   - `geometry_mismatch`: для каждой несопоставленной детекции — лучший несопоставленный osm-эталон; если IoU ≥
     `iou_mismatch_min` (0.1). При максимальном 1:1 назначении такая пара всегда имеет IoU < `iou_match`
     (иначе она была бы в `matches`). `geom` = объединение ref и det, `iou` = IoU пары.
   - `missing_in_map`: остальные несопоставленные (и не подавленные) детекции; `iou` = max IoU с любым osm-эталоном
     (NULL, если пересечений нет); `geom` = det. Сюда же попадает «вторая» детекция на один эталон.
   - `missing_on_image`: osm-эталоны без match и без пары mismatch; `iou` = max IoU с любой детекцией версии;
     `geom` = ref. Если передан AOI, эталоны, не пересекающие AOI, не рассматриваются (там нет снимка).
5. **Скоринг** — признаки в SQL (`geoqc_discrepancy_features`), формула в Python (`scoring.py`), запись
   `discrepancies.confidence` + `discrepancy_features`.

## Формулы

**IoU** (все площади — в метрической CRS `metric_srid` после `ST_Transform`):

```
IoU(A, B) = area(A ∩ B) / (area(A) + area(B) − area(A ∩ B));  IoU = 0 для непересекающихся
```

**Нормированная площадь** (площадь субъекта: det для `missing_in_map`/`geometry_mismatch`, ref для `missing_on_image`):

```
area_norm = clip( ln(area / min_area_m2) / ln(max_area_m2 / min_area_m2), 0, 1 )      # 20 м² → 0, 5000 м² → 1
```

**Второй источник**: `secondary_coverage = area(subject ∩ ⋃ footprints{microsoft,google}) / area(subject)`;
`agree_secondary` (для det) и `overlap_secondary` (для ref) — это одна и та же величина для соответствующего субъекта.

**Confidence** (веса `weights.<class>` из `config/matching.yaml`, каждый блок нормируется к сумме 1):

```
missing_in_map:     w_score·score + w_area·area_norm + w_sec·agree_secondary
missing_on_image:   w_area·area_norm + w_sec·(1 − overlap_secondary) + w_ctx·(1 − max_iou_any_det)
geometry_mismatch:  w_score·score + w_iou·(1 − iou) + w_area·area_norm
```

Веса по умолчанию: `missing_in_map {score 0.5, area 0.2, secondary 0.3}`, `missing_on_image {area 0.3, secondary 0.4,
context 0.3}`, `geometry_mismatch {score 0.4, iou 0.4, area 0.2}`. Если в `buildings_ref` нет ни одной строки
`microsoft`/`google`, член `secondary` выбрасывается, а остальные веса перенормируются (например,
`missing_in_map = (0.5·score + 0.2·area_norm) / 0.7`). `max_iou_any_det` — столбец `iou` у `missing_on_image` (NULL → 0).

**Граница AOI**: субъект, не покрытый AOI, сжатым на `aoi_edge_buffer_m` (10 м), получает флаг `aoi_edge`
(`discrepancy_features.aoi_edge`) и `confidence = confidence_raw · aoi_edge_factor` (0.5). Итог — `clip(·, 0, 1)`.

Проверочные числа (воспроизводятся в `tests/matching/test_scoring.py`):
`missing_in_map(score 0.8, area 200 м², agree 1.0) = 0.5·0.8 + 0.2·0.41702 + 0.3 = 0.783404`;
`geometry_mismatch(score 0.9, iou 1/3, area 316.2 м²) = 0.36 + 0.26667 + 0.1 = 0.72667`;
`missing_on_image(area 5000, overlap 0.25, max_iou 0.05) = 0.3 + 0.3 + 0.285 = 0.885`.

До вызова скоринга `geoqc_classify` пишет предварительный `confidence` (`score` / `1 − iou` / `0.5`), чтобы
SQL-функции были самодостаточны; `geoqc matching run` всегда перезаписывает его формулой выше.

## Параметры `config/matching.yaml`

| Ключ | По умолчанию | Смысл |
|---|---|---|
| `iou_match` | 0.5 | пара → `matches` |
| `iou_mismatch_min` | 0.1 | нижний порог `geometry_mismatch` |
| `dedup_iou` | 0.7 | порог NMS между детекциями |
| `algorithm` | `greedy` | `greedy` \| `hungarian` |
| `dedup_detections`, `split_multipart_detections` | true | шаги 1–2 |
| `suppress_small_detections` | true (в коде по умолчанию false) | подавление осколков и вложенных кусков, D-018 |
| `min_det_area_m2`, `nested_cover`, `nested_max_area_ratio` | 12, 0.8, 0.25 | пороги подавления |
| `suppress_ref_fragments`, `ref_fragment_cover` | true (в коде по умолчанию false), 0.8 | куски зданий OSM, D-025 |
| `aoi_path`, `aoi_edge_buffer_m`, `aoi_edge_factor` | `config/aoi.geojson`, 10, 0.5 | краевая обработка |
| `min_area_m2`, `max_area_m2` | 20, 5000 | нормировка площади |
| `weights.<class>` | см. выше | веса confidence |
| `export.path`, `export.precision` | `reports/discrepancies.geojson`, 7 | экспорт |

## SQL-примеры

```sql
-- IoU двух геометрий в UTM 37N
SELECT geoqc_iou(r.geom, d.geom, 32637) FROM buildings_ref r, buildings_det d WHERE r.id = 1 AND d.id = 1;

-- все пересекающиеся пары ref×det версии с IoU ≥ 0.1
SELECT * FROM geoqc_candidate_pairs('unet-abcd1234', 32637, 0.1) ORDER BY iou DESC;

-- полный прогон без Python (confidence будет предварительным)
SELECT geoqc_split_multipart_detections('unet-abcd1234');
SELECT geoqc_dedup_detections('unet-abcd1234', 32637, 0.7);
SELECT geoqc_match_greedy('unet-abcd1234', 32637, 0.5);
SELECT * FROM geoqc_classify('unet-abcd1234', 32637, 0.5, 0.1, NULL);   -- 5-й аргумент: AOI GeoJSON или NULL

-- признаки скоринга
SELECT * FROM geoqc_discrepancy_features('unet-abcd1234', 32637, NULL, 10) ORDER BY id;

-- очередь проверки
SELECT x.id, x.class, x.confidence, x.iou, r.osm_id FROM discrepancies x LEFT JOIN buildings_ref r ON r.id = x.ref_id
ORDER BY x.confidence DESC LIMIT 20;
```

## Производительность

Синтетическая сетка 4900 osm-эталонов × 4428 детекций (3929 пересекающихся пар), PostGIS 3.6 локально:
`run --algorithm greedy` ≈ 8 с, `hungarian` ≈ 4 с; только `geoqc_candidate_pairs` ≈ 0.8 с. Узкие места —
`ST_Transform` + `ST_Intersection` для каждой пары (считаются в дедупе, сопоставлении и классификации
независимо) и построчный цикл в `geoqc_match_greedy`. Для AOI 5–10 км² (единицы тысяч зданий) этого достаточно;
при росте на порядок: хранить метрическую копию геометрий, кэшировать пары в temp-таблице между шагами,
заменить цикл жадного на итеративный выбор взаимно лучших пар (set-based).

## Тесты

`pytest tests/matching -q` — 48 тестов: чистые (формулы скоринга на числах, жадный vs венгерский на матрице) и
`db` (фикстура `db_conn`, тестовая БД `geoqc_test`): IoU=1 → match, IoU=1/3 → `geometry_mismatch`, det без ref,
ref без det, две детекции на один эталон (без дедупа → вторая `missing_in_map`; с дедупом → подавлена),
мультиполигон det (по частям) и ref (целиком), второй источник не эталон, чужая `model_version` не трогается,
идемпотентность повторного `run`, конфликтный пример greedy (1 пара) vs hungarian (2 пары), граница AOI, NMS-цепочка,
экспорт по контракту, CLI через `CliRunner`. Геометрии строятся в метрах (UTM 32637) и переводятся в 4326;
из-за округления при обратном переводе IoU ровно на пороге (0.5) в тестах не используется.

## Зависимости, ограничения, что знать продолжающему

- Зависит от `geoqc.db`, `geoqc.config`; `scipy` только для `hungarian`; `shapely` — загрузка AOI.
- Пишет только в `matches`, `discrepancies`, `det_suppressed`, `discrepancy_features`; **кроме** шага 1:
  `split_multipart_detections` заменяет мультиполигонные строки `buildings_det` строками-частями (id меняются).
  Если CV/Eval полагаются на id детекций в БД — выключить в конфиге.
- Геометрии должны быть валидными (загрузчики Data/CV); `geoqc_iou` не вызывает `ST_MakeValid`.
- `geometry_mismatch` не обязан быть 1:1 — два det могут указывать на один ref (два предложения контура).
- `has_secondary` — глобальный признак (есть ли вообще строки `microsoft`/`google`); частичное покрытие AOI вторым
  источником занижает `agree_secondary` там, где его нет.
- Актуальные числа прогона на AOI — `geoqc matching stats` и `reports/discrepancies.geojson`.
