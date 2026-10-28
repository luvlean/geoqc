# Команды проекта

Справочник всех команд: CLI `geoqc`, цели `make` и скрипты экспериментов. Подробные параметры —
`geoqc <группа> <команда> --help`, контракты входов и выходов — [CONTRACTS.md](CONTRACTS.md) §6.

CLI ставится в виртуальное окружение (`make install`); запуск — `.venv/bin/geoqc …` или после
`source .venv/bin/activate` просто `geoqc …`.

## Цели make

Основной путь воспроизведения — `make all` (поднять базу, данные, детекция, сопоставление, оценка).

| Цель | Что делает |
|---|---|
| `make install` | создаёт `.venv`, ставит зависимости и пакет, копирует `.env.example` → `.env` |
| `make up` / `make down` | поднимает / останавливает PostGIS (docker compose; данные базы сохраняются) |
| `make db-init` | накатывает `sql/*.sql` на базу (`geoqc matching init-db`) |
| `make data` | снимок и эталоны: загрузка, предобработка, тайлы, OSM, Microsoft, загрузка в базу (`geoqc data all`) |
| `make labels-prep` | выбор тайлов, черновые контуры, проекты QGIS для val и test |
| `make labels-validate` | экспорт разметки из QGIS и её проверка → `reports/annotation.json` (`ANNOTATOR=…`) |
| `make detect` | проверка весов и детекция по всем тайлам → `detections.geojson` и `buildings_det` |
| `make match` | сопоставление с OSM, классификация расхождений, экспорт очереди, счётчики |
| `make eval` | метрики детекции (val; test — один раз), выборка на проверку, точность очереди, анализ ошибок, сравнение с Microsoft, гейты → `reports/metrics.json` |
| `make all` | `up db-init data detect match eval` подряд |
| `make demo` | веб-карта и API на `http://localhost:8000`; вкладка «Снимок» — детекция на любом загруженном снимке (перетащить файл или выбрать); `REVIEW=1` добавляет вкладку верификации |
| `make demo-docker` | то же через docker compose (собирает образ приложения) |
| `make test` / `make test-all` / `make test-cov` | быстрые тесты / все, включая сетевые и медленные / с покрытием, как в CI |
| `make lint` / `make format` | проверка / автоисправление `ruff` |
| `make notebooks-check` | `nbqa ruff` и `nbstripout --verify` по ноутбукам |
| `make ci` | локальный аналог GitHub Actions |
| `make docker-build`, `make compose-config` | сборка образа, проверка `docker-compose.yml` |
| `make pre-commit`, `make clean` | git-хуки, очистка кэшей (данные, модели и отчёты не трогаются) |
| `make help` | список целей |

## CLI `geoqc`

### `geoqc data` — снимок и эталонные контуры

| Команда | Что делает |
|---|---|
| `search-aoi` | ищет снимки в OpenAerialMap (сначала Россия), ранжирует → `reports/aoi_candidates.json` |
| `download` | загружает снимок в `data/raw`, sha256 → manifest |
| `preprocess` | перепроецирует в UTM, обрезает по AOI, RGB uint8, COG → `data/interim/aoi_cog.tif` |
| `tile` | режет снимок на тайлы 512×512 с перекрытием 64 px → `data/tiles/tiles.gpkg` |
| `osm` | здания OSM через Overpass → `data/interim/osm_buildings.geojson` |
| `secondary` | второй источник (footprints Microsoft или Google Open Buildings) |
| `load-ref` | загружает OSM и второй источник в `buildings_ref` |
| `check-georef` | оценивает сдвиг снимка относительно OSM → `reports/georef.json` (гейт G1) |
| `all` | все шаги подряд (готовые артефакты пропускаются, `--force` пересобирает) |

### `geoqc annotation` — ручная разметка

| Команда | Что делает |
|---|---|
| `select-tiles` | стратифицированный выбор тайлов разметки → `data/labels/tiles_split.csv` |
| `add-test-tiles` | добавляет новую тестовую выборку вдали от размеченных тайлов (D-021) |
| `pre-annotate` | черновые контуры на тайлах выборки → `data/labels/<split>/draft.geojson` |
| `qgis-project` | рабочий GeoPackage, рамки тайлов, стили и проект QGIS |
| `finalize` | `work.gpkg` → `buildings.geojson` / `recheck.geojson` (обрезка по тайлам, исправление геометрии) |
| `validate` | проверка разметки и согласия разметчиков → `reports/annotation.json` (гейт G2) |

### `geoqc cv` — модель детекции

| Команда | Что делает |
|---|---|
| `fetch-weights` | загружает или проверяет веса, считает sha256, пишет `model_version` в `config/cv.yaml` |
| `predict` | детекция по тайлам → `detections.geojson`; `--to-db` — ещё и в `buildings_det`; `--weights` — другие веса для экспериментов |
| `threshold-sweep` | подбор порога уверенности на val → `reports/threshold_sweep_val.json` |
| `export-dataset` | разметка → набор YOLO-seg (train / holdout); test не используется |

### `geoqc matching` — очередь расхождений

| Команда | Что делает |
|---|---|
| `init-db` | накатывает `sql/NNN_*.sql` (идемпотентно) |
| `run` | сопоставление детекций с OSM и классификация расхождений для версии модели |
| `export` | очередь → `reports/discrepancies.geojson`, по убыванию уверенности |
| `stats` | счётчики эталонов, детекций, совпадений и расхождений по классам |

### `geoqc eval` — метрики и гейты

| Команда | Что делает |
|---|---|
| `detection --split val\|test` | P/R/F1, mean IoU и F1 по площади → `reports/metrics_<split>.json`; test оценивается один раз |
| `verification-sample` | стратифицированная выборка расхождений на ручную проверку → `reports/verification_sample.csv` |
| `discrepancies --verification reports/verification.csv` | точность очереди по классам и Precision@N (гейты G6, G7) |
| `error-analysis` | ошибки по тайлам и стратам, распределения площади и IoU → `reports/error_analysis.md` |
| `compare-secondary` | модель и второй источник против ручной разметки → `reports/comparison.json` |
| `gates` | сводка `reports/metrics.json` и проверка G1–G9; флаги `--make-all-passed`, `--tests-passed --n-tests N` |

### `geoqc api` — веб-карта

| Команда | Что делает |
|---|---|
| `serve` | API и веб-карта (uvicorn); `--review` включает вкладку верификации; детекция на загруженном снимке — `POST /detect`, вкладка «Снимок» |
| `bench` | замер скорости `GET /discrepancies` на синтетических данных тестовой базы |

`geoqc version` — версия пакета.

## Скрипты экспериментов

Лежат в `scripts/experiments/`, запуск — `.venv/bin/python scripts/experiments/<имя>`. Конфиг и очередь не меняют.

| Скрипт | Что делает |
|---|---|
| `airs_night.py` | дообучение на своей разметке + AIRS, оценка на val (D-024) |
| `weighted_compare.py` | все модели на val по порогам, обычный и взвешенный по площади F1 → `reports/experiments/weighted_f1_val.json` |
| `build_experiments_notebook.py` | собирает `notebooks/experiments.ipynb` |
| `airs_test_eval.py` | оценка моделей на тестовой части AIRS → `reports/experiments/airs_test.json` |
| `refine_drafts.py`, `apply_test_review.py` | первичный проход по разметке test без модели (D-022) |

## Служебные скрипты

| Скрипт | Что делает |
|---|---|
| `scripts/bootstrap.sh` | окружение с нуля: `.venv`, зависимости, пакет, `.env` из примера (`make install`) |
| `scripts/db_init.py [--test]` | накатывает `sql/*.sql` на рабочую или тестовую базу (`make db-init-test`, CI) |
| `scripts/render_metrics_table.py table` | Markdown-таблицы детекции и гейтов из `reports/metrics.json` |
| `scripts/render_metrics_table.py fill docs/thesis/*.md --out-dir build/thesis` | главы записки с подставленными `{{metrics.*}}` / `{{manifest.*}}`; код 1, если ключа нет |
| `scripts/ci/check_notebooks.sh` | `nbqa ruff` и `nbstripout --verify` (`make notebooks-check`) |
