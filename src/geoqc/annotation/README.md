# Модуль `geoqc.annotation` — эталонная разметка

## Назначение

Готовит ручной эталон зданий для метрик детекции и проверяет его. Схема — предразметка плюс правка в QGIS (D-005):
студент не рисует с нуля, а проверяет и правит черновики. OSM в эталон не попадает, он используется только для стратификации тайлов и как визуальная подсказка.

Документы для человека: протокол [docs/annotation/PROTOCOL.md](../../../docs/annotation/PROTOCOL.md) и пошаговая инструкция [docs/annotation/INSTRUCTION_QGIS.md](../../../docs/annotation/INSTRUCTION_QGIS.md).

## Команды

```bash
geoqc annotation select-tiles                          # → data/labels/tiles_split.csv, recheck_tiles.csv, select_tiles_report.json
geoqc annotation pre-annotate --split val|test [--source secondary|detections] [--force]
                                                       # → data/labels/<split>/draft.geojson (verified=false)
geoqc annotation qgis-project --split val|test [--force] [--no-qgis] [--annotator NAME]
                                                       # → work.gpkg, qgis/*.qml, qgis/annotation_<split>.qgz
geoqc annotation finalize --split val|test [--annotator NAME] [--input work.gpkg]
                                                       # work.gpkg → buildings.geojson, recheck.geojson
geoqc annotation validate --split val|test [--strict]  # → reports/annotation.json, exit 1 при rejected
```

Все команды принимают `--labels-dir` и `--tiles-index` для тестов. Make-цели: `labels-prep`, `labels-validate`.

## Файлы

| Файл | Что делает |
|---|---|
| `common.py` | пути, чтение тайлов и сплита, метрическая CRS, поля меток `LABEL_PROPERTIES` |
| `select_tiles.py` | стратифицированный отбор тайлов и сплит val/test, подмножество повтора |
| `pre_annotate.py` | обрезка вторичных контуров или детекций по тайлам сплита |
| `qgis_project.py` | рабочий GeoPackage, стили `.qml`, спецификация слоёв |
| `_qgis_build_project.py` | запускается интерпретатором QGIS.app и собирает `.qgz` |
| `finalize.py` | нормализация: `make_valid`, MultiPolygon, привязка к тайлу, обрезка, фильтр площади |
| `validate.py` | проверки меток и G2 |
| `cli.py` | Typer-команды |

## Отбор тайлов

Страты по тайлу считаются по зданиям OSM: доля площади с тегами группы, плотность застройки и средняя площадь контура.

| Страта | Условие |
|---|---|
| `dense_multistorey` | доля площади с `apartments`, `dormitory` ≥ `dominant_share`, или без тегов: средняя площадь ≥ `large_footprint_m2` и плотность ≥ `dense_density` |
| `low_rise_private` | доля площади с `house`, `detached` и т. п. ≥ `dominant_share`, или без тегов: средняя площадь < `small_footprint_m2` |
| `industrial` | доля площади с `industrial`, `warehouse` и т. п. ≥ `dominant_share` |
| `mixed` | всё остальное |

Тайл-кандидат: не меньше `min_buildings_per_tile` зданий OSM и не меньше `min_aoi_coverage` площади внутри AOI.
Число тайлов на страту распределяется пропорционально числу кандидатов с нижней границей `min_per_stratum`.
Соседние тайлы не выбираются, потому что тайлы перекрываются на 64 пикселя.
Доля `test` берётся внутри каждой страты. Повтор — `ceil(recheck_fraction × n)`, но не меньше `recheck_min_tiles`.
Всё детерминировано зерном `seed`.

## Валидация и G2

Ошибки, из-за которых сплит получает статус `rejected`:
`missing_property`, `bad_split`, `bad_source`, `not_verified`, `geometry_type`, `empty_geometry`, `invalid_geometry`,
`min_area`, `unknown_tile`, `outside_tile`, `duplicate_overlap`.
Предупреждения: `small_overlap`, `tile_without_labels`, `recheck_*`, `g2_below_target`.

Дубль — пара с `|A ∩ B| / min(|A|, |B|) > overlap_error_ratio`. Площади считаются в метрической CRS.

G2 считается на тайлах повтора:

1. Объекты основной разметки и повтора сопоставляются 1:1 жадно по убыванию IoU.
2. `agreement_iou` — средний IoU пар, взвешенный по числу пар каждого сплита.
3. `agreement_f1 = 2·TP / (2·TP + FP + FN)`, где TP — пара с IoU ≥ `iou_match` (0.5).
4. Гейт G2: `agreement_iou ≥ 0.80` (`config/quality_gates.yaml`).

Полные формулы — в [docs/metrics_and_formulas.md](../../../docs/metrics_and_formulas.md).

## Контракт

Формат меток — `docs/CONTRACTS.md` §3.5: EPSG:4326, MultiPolygon, поля `tile_id, split, source, verified, annotator, notes`.
`source` ∈ `manual`, `pre_annotation`. В итоговом `buildings.geojson` все объекты имеют `verified=true`.

## Правила для агентов

- Модуль не читает `data/labels/test/` вне команд, которым явно передан `--split test`. CV Agent эти файлы не читает никогда.
- OSM не может быть источником предразметки: `pre-annotate` принимает только `secondary` и `detections`.
- `work.gpkg` не перезаписывается без `--force`, прошлая версия сохраняется как `.bak`.
- Тесты — `tests/annotation/`, синтетика. Сборка `.qgz` проверяется, только если установлен QGIS.app.
