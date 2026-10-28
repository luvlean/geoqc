# Карточка модели — детекция зданий (geoqc, модуль `cv`)

Формат — `docs/CONTRACTS.md §3.10`. Блок «Текущие веса» между маркерами перезаписывается автоматически
командой `geoqc cv fetch-weights` и финальной ячейкой `notebooks/train_finetune.ipynb`; остальной текст ведётся вручную.

## Текущие веса (автоблок)

<!-- weights:begin -->
| Поле | Значение |
|---|---|
| model_version | `yolov8m-building-seg-ft-ours-airs-f87c92a9` |
| backend | ultralytics |
| weights | `models/yolov8m-building-seg-ft-ours-airs.pt` |
| sha256 | `f87c92a91e5f8f6290b305b2a53d9a8d8e3065cd32da894e6021f75b0ab15c2b` |
| source | fine-tuned by scripts/experiments/airs_night.py from yolov8m-building-seg-1996bc04 |
| license | AGPL-3.0 (Ultralytics derivative); AIRS imagery/outlines LINZ CC BY 4.0, AIRS package licence unstated |
| fallback (COCO, no building class) | no |
| fetched_at | 2026-10-28T13:01:43+00:00 |
| fine-tune | AdamW lr0=2e-4, imgsz 768, 19 epochs, best epoch 9, early stopping on val; aerial + colour augmentation |
| train data | train (11 tiles, 126 buildings) x10 + AIRS 1318 chips at 0.3 m (23 452 buildings); val/test never used for training |
| inference | imgsz 768, conf 0.50 (chosen by area-weighted F1 on val, D-023) |
| backup | GitHub release weights-ours-airs-f87c92a9 (private) |
<!-- weights:end -->

## Источник весов

| | Основной вариант | Fallback |
|---|---|---|
| Чекпоинт | `keremberke/yolov8m-building-segmentation`, файл `best.pt` (54,8 МБ) | `yolov8m-seg.pt` (Ultralytics assets, COCO) |
| URL | https://huggingface.co/keremberke/yolov8m-building-segmentation | https://github.com/ultralytics/assets/releases |
| Лицензия | **В репозитории модели тег лицензии отсутствует** (проверено через `HfApi.model_info`, `license: None`). Веса — производная от `yolov8m-seg` Ultralytics (AGPL-3.0), поэтому фактически наследуют AGPL-3.0. Обучающий датасет — Roboflow Universe «Buildings Instance Segmentation» (зеркало `keremberke/satellite-building-segmentation`), лицензия **CC BY 4.0** | AGPL-3.0 (Ultralytics) |
| Классы | 1: `Building` | 80 классов COCO — **класса «здание» нет; без дообучения модель здания не детектирует**, используется только как архитектура/инициализация для ноутбука |
| Заявленное качество автора | mAP@0.5 (box) 0,623; mAP@0.5 (mask) 0,613 на valid-сплите их датасета (640 px). Цифры автора, не наши | — |
| sha256 `best.pt` | `1996bc046edf98fe7f5f63c0df8e5c7b61968437252042fb1394c027f2f9c656` | вычисляется при скачивании |
| model_version | `yolov8m-building-seg-1996bc04` | `yolov8m-seg-coco-<sha[:8]>` |

Проверка при выборе (2026-09-26): репозиторий существует, `best.pt` скачивается без токена, загружается в `ultralytics 8.4.163`
(`task=segment`, `names={0: 'Building'}`), инференс на MPS и CPU проходит. Из `train_args` чекпоинта: базовая модель
`yolov8m-seg.yaml`, `imgsz=640`, `epochs=3`, `batch=1`, `optimizer=SGD`, `lr0=0.01` — то есть чекпоинт дообучен автором очень
коротко; ожидать высокого качества на нашем AOI без дообучения не стоит (это и есть сценарий ноутбука).

## Архитектура

YOLOv8m-seg (Ultralytics, instance segmentation): backbone CSPDarknet-m, PAN-neck, детекционная голова + прототипы масок
(32 коэффициента), ~27 млн параметров. Альтернативный бэкенд — U-Net (`segmentation_models_pytorch`, энкодер `resnet34`,
ImageNet-инициализация) с разбиением семантической маски на связные компоненты (`backend: smp_unet`).

## Данные дообучения

- Только `data/labels/val/buildings.geojson` (объекты с `verified=true`) и тайлы `data/tiles/*.tif`, экспорт `geoqc cv export-dataset`.
- Внутри val — детерминированный стратифицированный сплит 80/20 (`dataset.seed`, `dataset.holdout_fraction`) на `train`
  и `holdout`; holdout используется для ранней остановки и подбора порога, в обучение не идёт.
- Разметка тестового сплита в модуле `cv` и ноутбуке **не читается и не используется ни на одном этапе** (запрет абсолютный;
  см. `tests/cv/test_no_test_labels.py`).
- Опционально: SpaceNet 2 (CC BY-SA 4.0, `DATA_SOURCE` в ноутбуке, `docs/datasets/spacenet2.md`), INRIA Aerial Image Labeling (ячейка в ноутбуке, выключена) — только из whitelist плана §9.

## Гиперпараметры инференса (`config/cv.yaml`)

| Параметр | Значение | Комментарий |
|---|---|---|
| `imgsz` | 512 | размер тайла, `retina_masks=True` |
| `conf_threshold` | 0,20 (sweep на val, 2026-10-24) | **подбирается только на val** командой `geoqc cv threshold-sweep --split val`; сплит и дата фиксируются в `conf_threshold_split` |
| `nms_iou` | 0,5 | NMS по боксам |
| `mask_threshold` | 0,5 | бинаризация маски |
| `vectorize.simplify_tolerance_m` | 0,5 м | Дуглас–Пекер |
| `vectorize.min_area_m2` | 12 | как `annotation.min_area_m2` |
| `vectorize.tile_overlap_merge_iou` / `min_overlap_ratio` | 0,1 / 0,5 | склейка на перекрытии тайлов |
| `device` | auto | cuda → mps → cpu; `half` только на CUDA |
| `seed` | 42 | инференс детерминирован |

Гиперпараметры дообучения (ноутбук): `epochs`, `batch`, `imgsz=512`, `amp=True`, `patience`, `seed=42`, `optimizer=auto`
(Ultralytics) или AdamW + cosine (U-Net) — фактические значения прогона дописываются в автоблок при экспорте весов.

## Метрики

Метрики детекции считает `geoqc eval detection` (`--split val|test` → `reports/metrics_*.json`): сопоставление 1:1,
IoU ≥ 0,5. С D-021 эталон `val` — 13 тайлов, 190 зданий, не участвовавших в обучении; модель и порог выбираются по
F1, взвешенному по площади (D-023). Все эксперименты — `notebooks/experiments.ipynb`, `reports/experiments/`.

| Модель | Вход | Порог | F1 по площади | F1 | Precision | Recall | mean IoU |
|---|---|---|---|---|---|---|---|
| базовая `yolov8m-building-seg-1996bc04` | 768 | 0,30 | 0,869 | 0,789 | — | — | 0,823 |
| текущая `…-ft-ours-airs-f87c92a9`, подбор порога | 768 | 0,50 | 0,978 | 0,847 | 0,881 | 0,816 | 0,835 |
| **текущая, `val` (пайплайн)** | 768 | 0,50 | **0,966** | **0,833** | 0,852 | 0,816 | 0,829 |
| **текущая, `test` (однократно)** | 768 | 0,50 | **0,926** | **0,812** | 0,834 | 0,791 | 0,807 |

Строка «подбор порога» — детекции, предсказанные отдельно по тайлам `val` (`scripts/experiments/weighted_compare.py`);
строки «пайплайн» — `make detect` по всему снимку со склейкой контуров соседних тайлов и `geoqc eval detection`, это официальные
G3–G5 (`reports/metrics.json`). Test (10 тайлов, 153 здания) оценён один раз после всех решений по модели и порогу.

Текущая модель находит все 74 здания `val` от 100 м² без ложных срабатываний среди крупных; слабое место —
постройки 12–30 м² (половина ошибок). Варианты с RID2 (D-024) равны по F1 по площади, но дают по одному ложному
крупному зданию; их веса — `models/yolov8m-building-seg-ft-ours-airs-rid2-{masked,full}.pt`.

## Окружение

Python 3.11, torch 2.14 (MPS), torchvision 0.29, ultralytics 8.4.163, segmentation-models-pytorch 0.5.0, rasterio 1.4.4,
geopandas 1.1.4, shapely 2.1.2. Обучение — Google Colab T4 или локально (MPS/CPU). Дата карточки: 2026-09-26.
