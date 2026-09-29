# Ноутбуки

## `train_finetune.ipynb` — дообучение детектора зданий

Универсальный ноутбук (`DECISIONS.md D-004`): один файл для локального запуска (CUDA / Apple MPS / CPU) и Google Colab.
Colab-ячейки помечены `# COLAB ONLY` и завёрнуты в `if IN_COLAB:`. Markdown — по-русски, код — по-английски, outputs в git
не хранятся (`nbstripout`), проверка — `nbformat.validate` и `nbqa ruff notebooks/`.

Тестовая разметка в ноутбуке не используется: обучение и валидация — только на разметке **val** (внутри неё сплит
`train`/`holdout` 80/20 с фиксированным seed), подбор порога — на `holdout`.

### Предварительные условия

1. Реальные тайлы `data/tiles/*.tif` + `tiles.gpkg` (`make data`) и `data/labels/tiles_split.csv` (`make labels-prep`).
2. Проверенная разметка `data/labels/val/buildings.geojson` (все объекты `verified=true`; `geoqc annotation validate --split val`).
3. Стартовые веса: `geoqc cv fetch-weights` (ноутбук скачает их сам, если файла нет).

### Локально

```bash
source .venv/bin/activate
geoqc cv export-dataset                 # можно пропустить: ноутбук сделает это сам
jupyter lab notebooks/train_finetune.ipynb
```

Run all. Параметры — в ячейке «6. Параметры обучения» (`TRAINER`, `EPOCHS`, `BATCH`, `RESUME`, `SMOKE_TEST`).
`SMOKE_TEST = True` (2 эпохи: при одной Ultralytics не делает прогрев lr) проверяет весь путь до экспорта весов за несколько минут.
Источник данных — `DATA_SOURCE` в ячейке «4. Данные»: `"val"` (наша проверенная разметка), `"spacenet"` (только SpaceNet 2:
прогон, пока разметки нет; `config/cv.yaml` и карточка не меняются, веса — `models/<name>-spacenet.pt`) или
`"val+spacenet"`. SpaceNet скачивается и готовится при первом запуске (`make external-data`, `docs/datasets/spacenet2.md`).

### Google Colab

```bash
geoqc cv bundle                          # -> data/interim/bundle.zip (тайлы, gpkg, labels/val, tiles_split.csv, config/, src/geoqc)
```

1. Загрузить `bundle.zip` на Google Drive (например, `MyDrive/geoqc/bundle.zip`) и `notebooks/train_finetune.ipynb` в Colab.
2. Runtime → Change runtime type → **GPU (T4)**.
3. В ячейке 2 указать `BUNDLE_ZIP`; Run all. Пакеты ставятся `pip`-ом внутри `if IN_COLAB:`, пакет `geoqc` импортируется из
   `src/` архива (без `pip install -e`).
4. Последняя ячейка копирует результаты на Drive в `MyDrive/geoqc/trained/<model_version>/`:
   `<model_name>.pt`, `cv.yaml`, `MODEL_CARD.md`, `threshold_sweep_val.json`, `results.csv`/`log.csv`.
5. Перенести в локальный репозиторий: веса → `models/`, `cv.yaml` → `config/`, `MODEL_CARD.md` → `models/`,
   `threshold_sweep_val.json` → `reports/`.

### Ожидаемое время

Оценки для ~20–25 val-тайлов 512×512 (≈16–20 train, 4–5 holdout), YOLOv8m-seg, `imgsz=512`:

| Среда | Эпоха | 60 эпох (с ранней остановкой обычно меньше) |
|---|---|---|
| Colab T4, `batch=8`, `cache='ram'`, AMP | ~10–20 с | 10–20 мин (+3–5 мин установка пакетов и распаковка) |
| Apple M3 Pro (MPS), `batch=4` | ~40–90 с | 40–90 мин |
| CPU, `batch=2` | 5–10 мин | часы — используйте `SMOKE_TEST` |

U-Net (`resnet34`) на тех же данных примерно в 1,5–2 раза быстрее на эпоху. Первый запуск дополнительно скачивает веса
(55 МБ HF-чекпоинт или ImageNet-энкодер).

### Что сделать после обучения

1. Убедиться, что `config/cv.yaml` содержит новые `backend`, `weights.path`/`smp_unet.checkpoint`, `model_version`,
   `conf_threshold` (записано ноутбуком), а `models/MODEL_CARD.md` — обновлённый автоблок.
2. `geoqc cv predict --split all --to-db` — детекции новой версией модели (`detections.geojson`, `buildings_det`).
3. `geoqc matching run --model-version <model_version>` → расхождения; `geoqc eval detection --split val` → гейт G3 (F1 ≥ 0,70).
4. Test-сплит оценивается один раз после фиксации весов и порога (`geoqc eval detection --split test`, `make eval`);
   ноутбук его не читает.
5. Если G3 не пройден: увеличить `EPOCHS`/`PATIENCE`, попробовать `TRAINER="unet"`, подмешать SpaceNet (`DATA_SOURCE="val+spacenet"`) или INRIA
   (ячейка 5, выключена по умолчанию) или использовать fallback плана §8 (Microsoft/Google footprints как «детектор»).

### Проверки

```bash
.venv/bin/python -c "import nbformat; nbformat.validate(nbformat.read('notebooks/train_finetune.ipynb', as_version=4))"
.venv/bin/nbqa ruff notebooks/
.venv/bin/nbstripout notebooks/train_finetune.ipynb   # перед коммитом, если ноутбук запускали
```
