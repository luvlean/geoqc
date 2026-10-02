# Пакет `geoqc` — ядро и связи модулей

Карта для агентов: что лежит в корне пакета, как модули передают друг другу данные и какие правила общие для всех.
Логика каждого модуля — в разделе «Логика модуля» его README.

## Ядро

| Файл | Логика |
|---|---|
| `cli.py` | Корневая Typer-группа `geoqc`. Саб-апп модуля (`<module>/cli.py: app`) регистрируется строкой в словаре `_SUBAPPS`; если модуля нет, он пропускается, и CLI всё равно импортируется. Тяжёлые зависимости (torch, ultralytics) импортируются лениво внутри команд. |
| `config.py` | `load_config()` собирает один словарь: `config/pipeline.yaml` на верхнем уровне, каждый `config/<module>.yaml` под ключом `<module>`, пороги `quality_gates.yaml` под `gates`. Каталог переопределяется `GEOQC_CONFIG_DIR` (так изолируются тесты). `resolve_path()` превращает путь из конфига в абсолютный, `repo_relative()` — обратно, для коммитимых отчётов. Секреты читает только `Settings` из `.env`. |
| `db.py` | `connect()` — контекст-менеджер psycopg 3 с dict-строками: commit при успехе, rollback при исключении; `test=True` берёт `TEST_DATABASE_URL`. `apply_schema()` применяет `sql/NNN_*.sql` по порядку, идемпотентно. SQL с пользовательским вводом — только через параметры. |

## Поток данных

```
data ──► data/tiles/*.tif, tiles.gpkg, buildings_ref, manifest.json, crs.metric
  │
  ├─► annotation ──► data/labels/tiles_split.csv, data/labels/<split>/buildings.geojson
  │                     │
  └─► cv ◄──────────────┘ (только val) ──► detections.geojson, buildings_det, model_version
                                              │
        matching ◄────────────────────────────┘ ──► matches, discrepancies (+ confidence)
           │
           ├─► api (+ web/) ──► очередь на карте, статусы, экспорт
           └─► eval ◄── labels val/test, verification.csv ──► reports/metrics.json (G1–G9)
```

Модули обмениваются только файлами и таблицами из `docs/CONTRACTS.md` (§3 — форматы, §6 — CLI, §8 — API); прямых импортов
между модулями почти нет. Где они есть, импорт опциональный, с локальным запасным вариантом: `utm_epsg_for` из `data.crs`,
метрики `eval.detection_metrics` в `cv.sweep`.

## Общие правила

- **Метрическая CRS** — `config/pipeline.yaml → crs.metric` (UTM, пишет `data preprocess`), запасной вариант —
  `data/manifest.json → aoi.utm_epsg`. Все площади, IoU и буферы считаются в ней; файлы на диске и БД — EPSG:4326.
- **`data/labels/test/`** читают только `annotation` (с `--split test`) и `eval`. В `cv` и ноутбуке путей к нему нет,
  это проверяет `tests/cv/test_no_test_labels.py`.
- **`model_version`** = `<model_name>-<sha256[:8]>` живёт в `config/cv.yaml`; детекции, `matches`, `discrepancies` и
  метрики помечены им. После смены весов: `fetch-weights` → `predict --to-db` → `matching run` → `eval`.
- **Идемпотентность.** Шаги пропускают готовые артефакты или заменяют свой слой целиком (`load-ref` — по `source`,
  `predict --to-db` — весь `buildings_det`, `matching run` — по `model_version`). Повтор не плодит дублей.
- **Отчёты** — только в `reports/`, с путями относительно репозитория. Числа в документах берутся из них.
- **Секреты** — только `.env`. Esri — только подложка для просмотра, не источник для детекции и обучения (D-002).
- **OSM** — проверяемый слой, не эталон: в разметку и обучение не попадает (D-005), правки в OSM не отправляются.

## Где менять

| Задача | Где |
|---|---|
| новая команда модуля | `src/geoqc/<module>/cli.py`, строка в `docs/CONTRACTS.md` §6, цель в `Makefile` при необходимости |
| новый параметр | `config/<module>.yaml` + значение по умолчанию в коде модуля |
| новая таблица или SQL-функция | новый `sql/NNN_*.sql` (применится `make db-init`) |
| новый модуль | папка `src/geoqc/<name>/` с `cli.py` и `README.md`, строка в `_SUBAPPS` (`cli.py`), тесты в `tests/<name>/` |
