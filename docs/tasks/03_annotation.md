# ТЗ T3 — Annotation Agent: инструменты разметки, протокол, инструкция для студента

**Цель.** Студент получает готовые черновые контуры и пошаговую инструкцию, правит их в QGIS за 1.5–2 часа, а скрипт валидации подтверждает готовность эталона и считает G2.

**Выходы.**
- `src/geoqc/annotation/`: `select_tiles.py` (стратифицированный отбор 20–30 тайлов: страты по доле площади зданий OSM с тегами apartments/house/industrial и плотности; сплит val/test без пересечения, зерно из конфига; → `data/labels/tiles_split.csv`), `pre_annotate.py` (черновые контуры из `data/interim/secondary_buildings.geojson` или `detections.geojson`, обрезка по тайлам выбранного split, `source=pre_annotation, verified=false` → `data/labels/<split>/draft.geojson`), `validate.py` (валидность геометрий, `verified=true` у всех, min area, отсутствие дубликатов/наложений > 50 %, соответствие `tile_id` сплиту; G2: сопоставление `buildings.geojson` vs `recheck.geojson` на recheck-тайлах → средний IoU пар и F1 → `reports/annotation.json`), `qgis_project.py` (генерация проекта: предпочтительно скрипт под PyQGIS `/Applications/QGIS.app/Contents/MacOS/bin/python3` создающий `.qgz` со слоями: COG, Esri World Imagery XYZ (если ключ), OSM-здания (только для подсказки, другой цвет), `draft.geojson` редактируемый; fallback — `.qml` стили + инструкция добавить слои вручную), `cli.py`.
- `config/annotation.yaml`: n_tiles, доля test, strata, min_area_m2 (например 12), recheck_fraction 0.1, seed.
- `docs/annotation/PROTOCOL.md` — протокол разметки: что считать зданием, пристройки, навесы, стройки, руины, здания под деревьями, минимальная площадь, контур по крыше vs по фундаменту (выбрать одно и обосновать смещением off-nadir), мультиполигоны, здания на границе тайла.
- `docs/annotation/INSTRUCTION_QGIS.md` — **инструкция для студента по шагам**: как открыть проект, какие слои, горячие клавиши редактирования (Add Polygon, Vertex Tool, Split, Merge), как выставлять `verified=true` через Field Calculator по фильтру, как делать слепую повторную разметку 10 % (recheck), как запустить `geoqc annotation validate --split val|test`, как читать `reports/annotation.json`, типичные ошибки. Оценка времени. Скриншоты не нужны, текст точный.
- `data/labels/README.md` — структура каталога и статусы.
- `tests/annotation/`: стратификация (детерминизм, непересечение сплитов), pre-annotate обрезка по тайлам, validate на синтетике (неверифицированный объект → ошибка; G2 на сдвинутых квадратах = 1/3 и т. п.).

**Входы, которые могут отсутствовать во время работы.** Реальные `tiles.gpkg`, `osm_buildings.geojson`, `secondary_buildings.geojson` появятся от Data Agent. Проектировать на синтетике; если реальные артефакты уже есть — прогнать `select-tiles` и `pre-annotate` на них и записать результат в LOG.

**DoD.** Команды работают на синтетике и (если данные есть) на реальных; инструкция полная; `validate` возвращает ненулевой код при непринятой разметке; README модуля.
