# Контракты проекта geoqc

Единый источник правды для всех агентов. Изменение контракта — только через координатора с записью в `DECISIONS.md`.
Код и комментарии — на английском; документация, README и описания тестовых кейсов — на русском.

## 1. Зоны ответственности (кто куда пишет)

| Агент | Пишет только сюда | Читает |
|---|---|---|
| Orchestrator (infra) | `Makefile`, `Dockerfile`, `docker-compose.yml`, `docker/`, `.github/`, `scripts/`, `.pre-commit-config.yaml`, `CONTRIBUTING.md`, `LICENSE`, `.editorconfig` | всё |
| Data | `src/geoqc/data/`, `config/data.yaml`, `config/aoi.geojson`, `data/raw/ interim/ tiles/`, `data/manifest.json`, `tests/data/`, `reports/aoi_candidates.json`, `reports/georef.json` | всё, кроме `data/labels/test/` |
| Annotation | `src/geoqc/annotation/`, `config/annotation.yaml`, `data/labels/`, `tests/annotation/`, `docs/annotation/`, `reports/annotation.json` | всё |
| CV | `src/geoqc/cv/`, `config/cv.yaml`, `notebooks/`, `models/`, `tests/cv/`, `data/interim/detections*.geojson`, `data/interim/masks/`, `data/interim/dataset/` | всё, **кроме `data/labels/test/`** (запрет абсолютный) |
| Geo | `sql/00[2-9]_*.sql`, `src/geoqc/matching/`, `config/matching.yaml`, `tests/matching/`, `reports/discrepancies*.geojson` | всё, кроме `data/labels/test/` |
| Eval | `src/geoqc/eval/`, `config/eval.yaml`, `tests/eval/`, `reports/` (metrics*.json, error_analysis.md, figures/, verification*.csv) | всё, включая `data/labels/test/` |
| App | `src/geoqc/api/`, `config/api.yaml`, `web/`, `tests/api/` | всё, кроме `data/labels/test/` |
| QA/Review | `tests/conftest.py`, `tests/integration/`, `docs/qa/` | всё, `data/labels/test/` только чтение |
| Docs | `README.md`, `docs/*.md` (кроме `CONTRACTS.md`, `tasks/`), `docs/thesis/`, `docs/presentation/` | всё |
| Координатор | `PLAN.md`, `DECISIONS.md`, `LOG.md` (структура), `docs/CONTRACTS.md`, `docs/tasks/`, `pyproject.toml`, `requirements*.txt`, `sql/001_schema.sql`, `src/geoqc/{__init__,config,db,cli}.py`, `config/pipeline.yaml`, `config/quality_gates.yaml` | всё |

Общие правила:
- Каждый модуль имеет `README.md` в своей директории `src/geoqc/<module>/README.md` — назначение, входы/выходы, команды, конфиг, формулы, зависимости от других модулей, известные ограничения. Пишется для агентов и инженеров.
- Не устанавливать пакеты (`pip install`) — окружение общее. Нужный пакет → записать в `LOG.md` в секции своей задачи, координатор добавит в `requirements.txt`.
- `pytest` и `ruff` запускать только на своих путях: `pytest tests/<module>`, `ruff format src/geoqc/<module> tests/<module>`, `ruff check --fix ...`.
- Никаких записей во внешние системы (OSM и т. п.). Сетевые загрузки — только из whitelist плана (§9).
- Никаких «выдуманных» цифр: любое число в отчёте порождено кодом и лежит в `reports/`.

## 2. Структура репозитория

```
Makefile  docker-compose.yml  Dockerfile  pyproject.toml  requirements.txt  requirements-dev.txt
config/            aoi.geojson  pipeline.yaml  quality_gates.yaml  <module>.yaml
data/              raw/  interim/  tiles/  labels/{val,test}/  manifest.json      (растры не в git)
sql/               001_schema.sql (контракт)  002+_*.sql (Geo)
src/geoqc/         config.py  db.py  cli.py  data/  annotation/  cv/  matching/  eval/  api/
web/               Leaflet SPA (статические файлы, отдаёт FastAPI по /)
notebooks/         train_finetune.ipynb (+ вспомогательные)
tests/             conftest.py  <module>/  integration/
reports/           metrics.json  metrics_val.json  metrics_test.json  error_analysis.md  figures/
docs/              CONTRACTS.md  tasks/  annotation/  qa/  thesis/  presentation/  *.md
models/            MODEL_CARD.md (+ веса, не в git)
PLAN.md  DECISIONS.md  LOG.md  README.md
```

Пакет устанавливается как `pip install -e .`; импорт — `from geoqc.<module> import ...`. Окружение: `.venv` (Python 3.11), активировать `source .venv/bin/activate` или вызывать `.venv/bin/python`.

## 3. Контракты данных

### 3.1 Системы координат
- Хранение (PostGIS, GeoJSON, GPKG): **EPSG:4326**.
- Площади, IoU, буферы, смещения: **метрическая UTM-зона AOI**, код записан в `config/pipeline.yaml → crs.metric` и в `data/manifest.json → aoi.utm_epsg` (заполняет Data Agent).
- Функция выбора зоны: `geoqc.data.crs.utm_epsg_for(lon, lat) -> int`.

### 3.2 Снимок
- `data/raw/<source>_<id>.tif` — исходник без изменений + `data/manifest.json`.
- `data/interim/aoi_cog.tif` — Cloud-Optimized GeoTIFF, RGB, uint8, обрезка по `config/aoi.geojson`, CRS = метрическая UTM-зона (для тайлинга без искажений), GSD ≤ 0.5 м.
- Если применён сдвиг геопривязки — он записан в `manifest.json → georef.shift_applied_m = [dx, dy]`.

### 3.3 Тайлы
- `data/tiles/{row}_{col}.tif`, 512×512 px, перекрытие 64 px, тот же CRS, что COG; края дополняются nodata=0.
- Индекс `data/tiles/tiles.gpkg`, слой `tiles`: `tile_id TEXT ("{row}_{col}"), row INT, col INT, path TEXT, geom Polygon (EPSG:4326)`.
- `tile_id` — единственный идентификатор тайла во всех модулях.

### 3.4 Манифест `data/manifest.json`
```json
{
  "aoi": {"name": "...", "area_km2": 0.0, "utm_epsg": 32600, "bbox_wgs84": [minx, miny, maxx, maxy]},
  "image": {"source": "openaerialmap|maxar_open_data|user", "id": "...", "url": "...", "acquired": "YYYY-MM-DD",
            "license": "CC-BY-4.0", "attribution": "...", "sha256": "...", "gsd_m": 0.0, "crs": "EPSG:xxxx"},
  "osm": {"snapshot_date": "YYYY-MM-DD", "query": "...", "count": 0, "license": "ODbL"},
  "secondary": {"source": "microsoft|google|null", "url": "...", "count": 0, "license": "ODbL|CC-BY-4.0"},
  "georef": {"n_control_points": 0, "median_offset_m": 0.0, "shift_applied_m": [0.0, 0.0]},
  "created_at": "ISO-8601"
}
```

### 3.5 Разметка (`data/labels/`)
- `data/labels/tiles_split.csv`: `tile_id,split,stratum,row,col` — `split ∈ {val,test}`, `stratum ∈ {dense_multistorey, low_rise_private, industrial, mixed}`. Тайлы val и test не пересекаются.
- `data/labels/{val,test}/buildings.geojson` — FeatureCollection, EPSG:4326, геометрия Polygon/MultiPolygon. Properties:
  `tile_id: str, split: str, source: "manual"|"pre_annotation", verified: bool, annotator: str, notes: str`.
  Обязательное условие приёмки: все объекты `verified = true`, геометрии валидны, площадь ≥ `annotation.min_area_m2`.
- `data/labels/{val,test}/draft.geojson` — черновая предразметка (не эталон), `verified = false`.
- `data/labels/{val,test}/recheck.geojson` — слепая повторная разметка 10 % тайлов для G2 (IoU-согласие).
- **`data/labels/test/` недоступен CV Agent и не участвует ни в обучении, ни в подборе порогов.** QA проверяет статически (grep по `src/geoqc/cv`, `notebooks/`).

### 3.6 Детекции
- `data/interim/detections.geojson` — FeatureCollection, EPSG:4326, MultiPolygon; properties `score: float [0,1], model_version: str, tile_id: str`.
- Таблица `buildings_det` (см. `sql/001_schema.sql`) заполняется командой `geoqc cv predict --to-db`.
- `model_version` — стабильная строка `"<model_name>-<weights_sha256[:8]>"`, записана в `models/MODEL_CARD.md` и `config/cv.yaml`.

### 3.7 База данных
DDL — `sql/001_schema.sql` (буквально контракт): `buildings_ref`, `buildings_det`, `matches`, `discrepancies`. Классы расхождений: `missing_in_map`, `missing_on_image`, `geometry_mismatch`; статусы: `new`, `confirmed`, `rejected`. Пары с IoU ≥ `iou_match` хранятся в `matches` (нужны для G5), не в `discrepancies`.

### 3.8 Экспорт очереди проверки
`reports/discrepancies.geojson` — FeatureCollection, каждая Feature = задача на проверку: геометрия расхождения, properties `id, class, confidence, iou, status, ref_osm_id, model_version, created_at`. Формат пригоден для загрузки в MapRoulette и открытия в JOSM.

### 3.9 Метрики `reports/metrics.json` (схема фиксирована)
```json
{
  "schema_version": "1.0", "generated_at": "ISO-8601", "git_commit": "sha", "git_dirty": false,
  "dataset": {"aoi": "...", "n_tiles_val": 0, "n_tiles_test": 0, "n_gt_val": 0, "n_gt_test": 0},
  "model": {"model_version": "...", "conf_threshold": 0.0, "iou_threshold": 0.5},
  "georeference": {"median_offset_m": 0.0, "n_control_points": 0},
  "annotation": {"agreement_iou": 0.0, "agreement_f1": 0.0, "n_checked": 0},
  "detection": {
    "val":  {"tp": 0, "fp": 0, "fn": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0, "mean_iou_matched": 0.0},
    "test": {"tp": 0, "fp": 0, "fn": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0, "mean_iou_matched": 0.0}
  },
  "discrepancies": {
    "counts_by_class": {"missing_in_map": 0, "missing_on_image": 0, "geometry_mismatch": 0},
    "verified_by_class": {"missing_in_map": {"n": 0, "true_error": 0, "precision": 0.0}, "...": {}},
    "precision_overall": 0.0,
    "precision_at_n": {"n": 20, "value": 0.0}
  },
  "comparison": {"secondary_source": "microsoft", "detection_vs_secondary": {"precision": 0.0, "recall": 0.0, "f1": 0.0}},
  "reproducibility": {"make_all_passed": false}, "tests": {"passed": false, "n_tests": 0},
  "gates": {"G1": {"target": 2.0, "op": "<=", "actual": 0.0, "passed": false, "note": ""}, "...": {}}
}
```
Промежуточные файлы: `reports/metrics_val.json`, `reports/metrics_test.json`, `reports/georef.json`, `reports/annotation.json`, `reports/verification_sample.csv` (для ручной проверки студентом), `reports/verification.csv` (заполненный).

### 3.10 Карточка модели `models/MODEL_CARD.md`
Источник весов (URL, лицензия), архитектура, данные дообучения (без `labels/test`), гиперпараметры, `model_version`, порог уверенности и на каком split подобран, дата, окружение.

## 4. Формулы (базовые определения; Docs Agent раскрывает в `docs/metrics_and_formulas.md`)

- IoU(A,B) = area(A∩B) / area(A∪B), площади в метрической CRS.
- Сопоставление 1:1 детекций и эталона: пары по убыванию IoU (жадно) или венгерский алгоритм по матрице IoU; пара засчитывается при IoU ≥ 0.5.
- TP = число пар, FP = детекции без пары, FN = эталон без пары. Precision = TP/(TP+FP), Recall = TP/(TP+FN), F1 = 2PR/(P+R).
- mean_iou_matched = среднее IoU по засчитанным парам.
- Классы расхождений (пороги в `config/matching.yaml`): `match` IoU ≥ 0.5; `geometry_mismatch` 0.1 ≤ IoU < 0.5; `missing_in_map` — детекция без пары (max IoU < 0.1); `missing_on_image` — эталон без пары.
- Уверенность расхождения `confidence ∈ [0,1]` — взвешенная комбинация признаков: уверенность модели, нормированная площадь, (1 − IoU), согласие со вторым источником. Веса в `config/matching.yaml`, точная формула — в `src/geoqc/matching/README.md`.
- Precision расхождений = доля подтверждённых реальных ошибок карты среди проверенных, по каждому классу и в целом.
- Precision@N = доля реальных ошибок среди топ-N расхождений по `confidence`.
- Согласие разметки (G2) = средний IoU между парами объектов первой и повторной разметки на 10 % тайлов + F1 между ними.
- Смещение геопривязки (G1) = медиана расстояний между контрольными точками на снимке и в векторе, м.

## 5. Конфигурация
`config/pipeline.yaml` — глобальные пути, CRS, тайлинг (координатор). `config/<module>.yaml` — параметры модуля (владелец модуля). Загрузка: `from geoqc.config import load_config; cfg = load_config(); cfg["matching"]["iou_match"]`. Секреты — только `.env` (см. `.env.example`), доступ через `geoqc.config.get_settings()`.

## 6. CLI (обязательные команды — по ним пишется Makefile)
Каждый модуль создаёт `src/geoqc/<module>/cli.py` с `app = typer.Typer()`; корневой `geoqc` подключает его автоматически.

| Модуль | Команды |
|---|---|
| data | `search-aoi` → `reports/aoi_candidates.json`; `download`; `preprocess` (COG, обрезка, сдвиг); `tile`; `osm` → `data/interim/osm_buildings.geojson`; `secondary` (MS/Google footprints, если есть); `load-ref` (в `buildings_ref`); `check-georef` → `reports/georef.json`; `all` |
| annotation | `select-tiles` → `data/labels/tiles_split.csv`; `add-test-tiles` (D-021); `pre-annotate --split train|val|test --source secondary|detections` → `draft.geojson`; `qgis-project` (генерирует проект/слои для QGIS); `finalize --split [--annotator]` (`work.gpkg` → `buildings.geojson`, `recheck.geojson`); `validate --split` → `reports/annotation.json` |
| cv | `fetch-weights`; `predict --split all|train|val|test [--to-db] [--weights]` → `detections.geojson`; `export-dataset` (labels train → обучение, val → holdout, D-021); `threshold-sweep --split val` → `reports/threshold_sweep_val.json` |
| matching | `init-db` (применяет `sql/`); `run --model-version` → `matches`, `discrepancies`; `export` → `reports/discrepancies.geojson`; `stats` |
| eval | `detection --split val|test` → `reports/metrics_<split>.json`; `verification-sample` → `reports/verification_sample.csv`; `discrepancies --verification reports/verification.csv`; `error-analysis` → `reports/error_analysis.md`; `compare-secondary`; `gates` → `reports/metrics.json` |
| api | `serve [--host --port] [--review]`; `bench` (скорость `GET /discrepancies` на тестовой БД) |

## 7. Make-таргеты (Orchestrator)
`install` `up` `down` `db-init` `data` `labels-prep` `detect` `match` `eval` `demo` `all` `test` `lint` `ci` `clean`.
`all` = `up db-init data detect match eval`. Совместимость с GNU make 3.81 (macOS): без `.ONESHELL` и функций make 4.x.

## 8. API (App Agent)
- `GET /health` → `{"status":"ok","db":true}`
- `GET /discrepancies?bbox=minx,miny,maxx,maxy&class=a,b&min_conf=0.0&status=new,confirmed&limit=500&offset=0` → GeoJSON FeatureCollection, сортировка по `confidence DESC`
- `GET /discrepancies/{id}` → Feature; `PATCH /discrepancies/{id}` body `{"status":"confirmed"|"rejected"|"new"}` → Feature
- `GET /tiles/{z}/{x}/{y}.mvt` → слои `discrepancies`, `buildings_ref`, `buildings_det` через `ST_AsMVT`
- `GET /imagery/{z}/{x}/{y}.png` → тайлы COG (rio-tiler), подложка «снимок»
- `GET /export?class=&min_conf=&status=` → GeoJSON (контракт 3.8)
- Детекция на загруженном снимке (`detect.enabled`): `GET /detect/config`; `POST /detect` multipart `file` (+ `gsd_m`, `conf`) → превью, контуры в пикселях и, для снимка с привязкой, в EPSG:4326; в базу не пишет
- Режим тестировщика (`review.enabled` в `config/api.yaml` или `geoqc api serve --review`): `GET /verification` → выборка ручной проверки с вердиктами; `PATCH /verification/{id}` body `{"verdict": "map_error"|"model_error"|"image_issue"|"unsure"|null, "comment"}` → пишет только `verdict`/`comment` в `reports/verification.csv`, состав выборки не меняется
- `GET /stats` → счётчики по классам/статусам; `GET /config/map` → центр/bbox AOI, атрибуции, `esri_enabled`
- Статика `web/` по `/`. Ответ на AOI < 1 с.

## 9. Тесты
`tests/<module>/test_*.py`; маркеры `db`, `network`, `slow`, `gpu` (см. `pyproject.toml`), db-тесты используют фикстуру `db_conn` (тестовая БД `geoqc_test`). Описание кейса — русская docstring; код и комментарии — английские. Синтетические геометрии — обязательны для сопоставления и классификации.

## 10. Журнал `LOG.md`
По завершении задачи агент дописывает **одним** `cat >> LOG.md` блок:
```
## [<дата>] <Agent> — <задача>
- Сделано: ...
- Решения: ...
- Не получилось / риски: ...
- Нужны пакеты / изменения контрактов: ...
- Команды проверки: ...
```
