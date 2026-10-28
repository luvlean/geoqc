# Ноутбуки

## `airs_eda.ipynb` — анализ внешнего датасета AIRS

Состав, покрытие крышами, размеры и форма зданий, цвет — в сравнении с нашей разметкой `train`/`val`; выводы в
последней ячейке и в `docs/datasets/airs.md`. Путь к данным — переменная `AIRS_DIR` (по умолчанию
`data/raw/external/airs`). Рисунки пишутся в `docs/datasets/figures/airs_*`, тяжёлые
расчёты кэшируются в `data/interim/external/airs/eda/`. Хранится **с выводами**
(метаданные `keep_output: true`: `nbstripout` оставляет графики и убирает только номера и время запусков).

## `experiments.ipynb` — все эксперименты с моделью

Сводная таблица F1/P/R по всем попыткам улучшить модель (внешние датасеты, дообучение на своей разметке,
аугментации, масштаб входа, AIRS), F1 по порогам, кривые обучения, полнота и точность по размерам зданий и выводы.
Только читает готовые результаты (`reports/experiments/`, `data/interim/experiments/`); хранится с выводами
(`keep_output: true`). Рисунки — `docs/figures/experiments_*.png`.

## `train_finetune.ipynb` — дообучение детектора зданий

Локальный запуск (CUDA / Apple MPS / CPU, устройство выбирается само). Markdown — по-русски, код — по-английски, outputs в
git не хранятся (`nbstripout`), проверка — `nbformat.validate` и `nbqa ruff notebooks/`.

Обучение — на своей разметке (D-021): тайлы `train` — обучение, тайлы `val` — holdout для ранней остановки и подбора порога.
Тестовая разметка в ноутбуке не используется. Рабочая модель (train + AIRS, D-024) обучена по той же схеме скриптом
`scripts/experiments/airs_night.py`.

### Предварительные условия

1. Реальные тайлы `data/tiles/*.tif` + `tiles.gpkg` (`make data`) и `data/labels/tiles_split.csv` (`make labels-prep`).
2. Проверенная разметка `data/labels/train/buildings.geojson` и `data/labels/val/buildings.geojson` (все объекты
   `verified=true`; `geoqc annotation validate`).
3. Стартовые веса: `geoqc cv fetch-weights` (ноутбук скачает их сам, если файла нет).

### Запуск

```bash
source .venv/bin/activate
geoqc cv export-dataset                 # можно пропустить: ноутбук сделает это сам
jupyter lab notebooks/train_finetune.ipynb
```

Run all. Параметры — в ячейке «4. Параметры обучения» (`EPOCHS`, `BATCH`, `RESUME`, `SMOKE_TEST`).
`SMOKE_TEST = True` (2 эпохи: при одной Ultralytics не делает прогрев lr) проверяет весь путь до экспорта весов за несколько минут.

### Ожидаемое время

Оценки для 11 тайлов `train` и 13 тайлов `val` 512×512, YOLOv8m-seg:

| Среда | Эпоха | 60 эпох (с ранней остановкой обычно меньше) |
|---|---|---|
| CUDA (T4 и выше), `batch=8`, AMP | ~10–20 с | 10–20 мин |
| Apple M3 Pro (MPS), `batch=4` | ~40–90 с | 40–90 мин |
| CPU, `batch=2` | 5–10 мин | часы — используйте `SMOKE_TEST` |

Первый запуск дополнительно скачивает стартовые веса (55 МБ, HF).

### Что сделать после обучения

1. Убедиться, что `config/cv.yaml` содержит новые `weights.path`, `model_version`, `conf_threshold` (записано ноутбуком),
   а `models/MODEL_CARD.md` — обновлённый автоблок.
2. `geoqc cv predict --split all --to-db` — детекции новой версией модели (`detections.geojson`, `buildings_det`).
3. `geoqc matching run --model-version <model_version>` → расхождения; `geoqc eval detection --split val` → гейт G3 (F1 ≥ 0,70).
4. Test-сплит оценивается один раз после фиксации весов и порога (`geoqc eval detection --split test`, `make eval`);
   ноутбук его не читает.

### Проверки

```bash
.venv/bin/python -c "import nbformat; nbformat.validate(nbformat.read('notebooks/train_finetune.ipynb', as_version=4))"
.venv/bin/nbqa ruff notebooks/
.venv/bin/nbstripout notebooks/train_finetune.ipynb   # перед коммитом, если ноутбук запускали
```
