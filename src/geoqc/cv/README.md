# Модуль `geoqc.cv` — детекция зданий

Назначение: по тайлам `data/tiles/*.tif` получить контуры зданий (`data/interim/detections.geojson`, таблица `buildings_det`),
подготовить данные для дообучения, подобрать порог уверенности и собрать архив для Colab. Контракты: `docs/CONTRACTS.md`
§3.3 (тайлы), §3.5 (разметка), §3.6 (детекции), §3.10 (карточка модели), §4 (формулы), §6 (CLI).

**Запрет.** Модуль никогда не читает разметку сплита `test` (каталог `data/labels/<split>/` для `split=test`): нет ни одного
пути к нему в коде и ноутбуках (`tests/cv/test_no_test_labels.py`), `assert_not_forbidden()` отклоняет такие пути в
`export-dataset`, `bundle` проверяет каждое имя в архиве, `threshold-sweep` принимает только `--split val`.
Инференс `predict --split test` по **тайлам** (растру) разрешён — это нужно модулю `eval`.

## Логика модуля

Общий слой: `tiles.py` (индекс и чтение тайлов) и `predictor.py` (модель за интерфейсом `Predictor`). Поверх него —
независимые сценарии:

| Сценарий | Команда | Цепочка |
|---|---|---|
| веса | `fetch-weights` | `weights.fetch_weights`: если файл есть, только считается хеш; если нет — скачивание с HF Hub, иначе (если разрешено) COCO-чекпоинт Ultralytics. Затем `model_version_for` → `update_cv_config` + `update_model_card` (уже записанные в автоблок сведения о дообучении сохраняются) |
| детекция | `predict` | `pipeline.run_predict`: `load_tile_index` → `filter_by_split` → `predict_tiles_raw` (батчи `read_tile` → `Predictor.predict_batch` → `vectorize_masks`) → `vectorize.postprocess` (склейка на перекрытиях, Дуглас–Пекер, фильтр площади) → `write_detections`; с `--to-db` ещё `load_to_db` |
| датасет | `export-dataset` | `dataset.export_dataset`, только `val`: `split_tiles` (train/holdout по стратам, seed) → `polygon_to_yolo_lines` + `rasterize_mask` → `data/interim/dataset/` |
| внешний датасет | `fetch-external`, `prepare-external`, `report-external` | `external.fetch_spacenet` (листинг S3 → детерминированная выборка → загрузка с кэшем) → `prepare_spacenet` (`prepare_chip`: UTM 0,3 м, 512 px, растяжка p2–p98, обрезка полигонов) → тот же формат, что у `export-dataset`; `external_report.build_report` строит статистику и рисунки |
| порог | `threshold-sweep` | `sweep.run_sweep`: один инференс при `conf_floor` → для каждого порога фильтр масок и тот же `postprocess` → `evaluate` (метрики из `eval.detection_metrics`) → `pick_best` → `conf_threshold` в `cv.yaml` |
| Colab | `bundle` | `bundle.build_bundle`: тайлы, разметка val, конфиги, исходники, `data/interim/dataset/` → zip; каждое имя проверяется `is_forbidden_arcname`. SpaceNet в архив не входит, ноутбук скачивает его сам |

Само обучение идёт не в модуле, а в `notebooks/train_finetune.ipynb`. Ноутбук импортирует `geoqc.cv`, всегда стартует с
исходных весов `models/yolov8m-building-seg.pt` и записывает веса, `model_version`, `conf_threshold` и карточку модели.

Инварианты:

- разметка test недоступна (см. «Запрет» выше);
- геометрия считается в UTM тайла, на выходе — EPSG:4326, MultiPolygon, поля `score, model_version, tile_id`;
- инференс детерминирован (`seed_everything`), бэкенд меняется только через `build_predictor`;
- `predict --to-db` заменяет весь слой `buildings_det`: в БД одна модель.

Где менять: новый бэкенд — `predictor.py` + `build_predictor`; постобработка — `vectorize.py` и `config/cv.yaml →
vectorize`; формат датасета — `dataset.py` (SpaceNet повторяет его в `external.py`); состав архива — `bundle.py`.

## Входы и выходы

| Вход | Откуда |
|---|---|
| `data/tiles/{row}_{col}.tif` (512×512, overlap 64, UTM, nodata 0) + `data/tiles/tiles.gpkg` | `geoqc data tile`; без gpkg индекс восстанавливается по именам файлов |
| `data/labels/tiles_split.csv` (`tile_id,split,stratum,row,col`) | `geoqc annotation select-tiles` |
| `data/labels/val/buildings.geojson` (`verified=true`) | студент в QGIS |
| веса `models/*.pt` | `geoqc cv fetch-weights` или ноутбук |

| Выход | Команда |
|---|---|
| `data/interim/detections.geojson` (`detections_<split>.geojson` для val/test): EPSG:4326, MultiPolygon, `score, model_version, tile_id` | `predict` |
| строки `buildings_det` (замена всего слоя: прежние детекции любых версий, `matches` и `discrepancies` удаляются) | `predict --to-db` |
| `data/interim/dataset/` — YOLO-seg + маски, `data.yaml`, `split.csv`, `meta.json` | `export-dataset` |
| `reports/threshold_sweep_val.json`, `config/cv.yaml → conf_threshold` | `threshold-sweep` |
| `data/interim/bundle.zip` | `bundle` |
| `config/cv.yaml → model_version, weights.sha256`, автоблок `models/MODEL_CARD.md` | `fetch-weights` |

## Пайплайн `predict`

```
tiles.gpkg ─► filter_by_split ─► read_tile (RGB uint8, nodata-маска)
          ─► Predictor.predict_batch ─► list[Mask] (bool H×W + score)
          ─► vectorize_masks: rasterio.features.shapes + affine → полигоны в CRS тайла (UTM), make_valid, вырезание nodata
          ─► merge_overlaps: склейка кусков одного здания с соседних тайлов (union-find)
          ─► simplify_detections: Дуглас–Пекер, tolerance в метрах
          ─► filter_min_area ─► as_multipolygon ─► to_crs(EPSG:4326) ─► GeoJSON / buildings_det
```

Файлы: `tiles.py` (индекс/чтение), `predictor.py` (интерфейс и бэкенды), `vectorize.py` (геометрия), `pipeline.py`
(`run_predict`, `write_detections`, `load_to_db`), `weights.py`, `dataset.py`, `sweep.py`, `bundle.py`, `cli.py`.

### Интерфейс `Predictor`

```python
class Predictor(ABC):
    def predict(self, tile: np.ndarray) -> list[Mask]: ...  # tile: H×W×3 uint8 RGB
    def predict_batch(self, tiles: list[np.ndarray]) -> list[list[Mask]]: ...
```

| backend (`config/cv.yaml`) | Класс | Веса | Примечание |
|---|---|---|---|
| `ultralytics` | `UltralyticsSegPredictor` | `weights.path` | YOLOv8-seg, `retina_masks=True`, `half` на CUDA, вход передаётся как BGR (конвенция Ultralytics для numpy) |
| `smp_unet` | `SmpUnetPredictor` | `smp_unet.checkpoint` | U-Net → sigmoid → `mask_threshold` → 8-связные компоненты; score = средняя вероятность компоненты |
| `mock` | `MockPredictor` | — | яркие пиксели (все каналы ≥ 200) → компоненты; score = 0,5 + 0,5·яркость/255. Для тестов и smoke-прогонов |

Устройство: `device: auto` → `cuda` → `mps` → `cpu`. Seed фиксируется (`seed_everything`), инференс детерминирован
(`tests/cv/test_pipeline.py::test_predict_is_deterministic`).

## Формулы

**Полигонизация.** Пиксель `(col,row)` → координаты через аффинное преобразование тайла `T`: `(x, y) = T · (col, row)`;
контуры — `rasterio.features.shapes` (4-связность), объединение частей одной маски `unary_union`, `make_valid`.
Пиксели nodata (все каналы = 0) исключаются из маски до полигонизации.

**Склейка на перекрытии тайлов** (`merge_overlaps`). Для пары детекций `A`, `B` из *разных* тайлов
(`merge_same_tile: false`):

```
IoU(A,B)   = area(A∩B) / area(A∪B)
ratio(A,B) = area(A∩B) / min(area(A), area(B))
link(A,B)  = IoU ≥ tile_overlap_merge_iou  OR  ratio ≥ min_overlap_ratio
```

Связи транзитивны (union-find): здание, разрезанное 3–4 тайлами, собирается в одну группу. Геометрия группы —
`unary_union`; score — `max(score_k)` (`score_mode: max`) или `Σ score_k·area_k / Σ area_k` (`area_weighted`);
`tile_id` — тайл самого большого куска. Зачем два критерия: у крупного здания, лишь краем попавшего в соседний тайл,
IoU кусков мал (например, 0,16 при ширине 100 м и полосе перекрытия 32 м), а `ratio` близок к 1 — поэтому IoU-порог
низкий (0,1), а `ratio` ≥ 0,5 ловит остальное. Пары внутри одного тайла не склеиваются — их уже разрешил NMS.

**Упрощение Дугласа–Пекера** (`simplify_detections`): `shapely.simplify(tolerance, preserve_topology=True)` — рекурсивно
удаляются вершины, отстоящие от хорды меньше чем на `tolerance` (м); `simplify_tolerance_m: 0.5` = 1 пиксель при GSD 0,5 м,
убирает «лестницу» пиксельных контуров, площадь меняется < 5 %.

**Фильтр площади:** `area(P) ≥ min_area_m2` (12 м², как `annotation.min_area_m2`), площадь в UTM.

**model_version** = `"<model_name>-<sha256(weights)[:8]>"` (§3.6).

**Метрики sweep** (`sweep.py`, при отсутствии `geoqc.eval.detection_metrics`): жадное 1:1 сопоставление по убыванию IoU,
пара при IoU ≥ 0,5; `TP` = пары, `FP` = детекции без пары, `FN` = эталон без пары; `P = TP/(TP+FP)`, `R = TP/(TP+FN)`,
`F1 = 2PR/(P+R)`, `mean_iou_matched` — средний IoU пар. Выбор порога: max F1, при равенстве — большая precision, затем
больший порог.

## Команды CLI

```bash
geoqc cv fetch-weights [--force] [--no-fallback]
geoqc cv predict --split all|val|test [--to-db] [--limit N] [--out path] [--device auto|cuda|mps|cpu] [--conf 0.3] [--backend mock] [--append]
geoqc cv export-dataset [--labels data/labels/val/buildings.geojson] [--out data/interim/dataset] [--seed 42] [--holdout-fraction 0.2] [--allow-unverified]
geoqc cv threshold-sweep --split val [--holdout] [--detections data/interim/detections_val.geojson] [--limit N] [--no-update-config] [--allow-unverified]
geoqc cv bundle [--tiles all|val] [--out data/interim/bundle.zip] [--no-dataset]
geoqc cv fetch-external [--city AOI_3_Paris ...] [--per-city 100]
geoqc cv prepare-external [--city ...] [--out data/interim/external/spacenet2]
geoqc cv report-external [--per-city 4]
```

Примеры: `geoqc cv predict --split all --to-db` (полный прогон, как в `make detect`);
`geoqc cv predict --split val --backend mock --limit 2` (smoke без весов);
`geoqc cv threshold-sweep --split val --holdout` (после дообучения — только на holdout-части val).
`--allow-unverified` нужен лишь для smoke-прогонов на `draft.geojson`, метрики по нему — не метрики.
`*-external` — SpaceNet 2 (`external.py`, `external_report.py`, блок `external:` в `config/cv.yaml`, `make external-data`):
загрузка из публичного S3, перепроекция в UTM 0,3 м, чипы 512 px в формате `export-dataset`, рисунки и статистика.
Описание и выводы — `docs/datasets/spacenet2.md`.

## Параметры `config/cv.yaml`

| Ключ | Значение по умолчанию | Смысл |
|---|---|---|
| `backend` | `ultralytics` | `ultralytics` / `smp_unet` / `mock` |
| `model_name`, `model_version` | `yolov8m-building-seg`, заполняется | префикс и итоговая версия |
| `weights.path/hf_repo/hf_filename/fallback_asset/sha256/is_fallback` | `keremberke/yolov8m-building-segmentation`, `best.pt`, `yolov8m-seg.pt` | источник весов; `sha256`/`is_fallback` пишет `fetch-weights` |
| `conf_threshold`, `conf_threshold_split` | 0,25, `null` | порог; на каком сплите/когда подобран (пишет `threshold-sweep`) |
| `nms_iou`, `imgsz`, `batch`, `device`, `half_on_cuda`, `max_det`, `seed`, `mask_threshold` | 0,5, 512, 8, auto, true, 300, 42, 0,5 | инференс |
| `vectorize.*` | `simplify_tolerance_m 0.5`, `min_area_m2 12`, `tile_overlap_merge_iou 0.1`, `min_overlap_ratio 0.5`, `score_mode max`, `merge_same_tile false`, `drop_nodata true` | постобработка |
| `smp_unet.encoder/checkpoint/min_component_px` | `resnet34`, `models/unet_resnet34.pt`, 16 | бэкенд U-Net |
| `dataset.out_dir/holdout_fraction/seed` | `data/interim/dataset`, 0,2, 42 | `export-dataset` |
| `sweep.conf_floor/thresholds/iou_threshold/report` | 0,05, 0,05…0,90, 0,5, `reports/threshold_sweep_val.json` | `threshold-sweep` |
| `bundle.out/tiles` | `data/interim/bundle.zip`, `all` | `bundle` |

## Смена бэкенда / весов

1. Ultralytics-чекпоинт (свой или дообученный): положить `.pt` в `models/`, прописать `weights.path`, выполнить
   `geoqc cv fetch-weights` (файл уже есть — сеть не нужна; пересчитает sha256 → `model_version`, обновит карточку).
   Если sha256 и `model_version` в конфиге уже совпадают с файлом (экспорт из ноутбука), конфиг и карточка не меняются.
2. U-Net: `backend: smp_unet`, `smp_unet.checkpoint` — файл формата `{"state_dict", "arch", "encoder", "classes", "mean", "std"}`
   (его пишет ноутбук); `model_version` = `unet-<encoder>-<sha[:8]>` (ноутбук записывает в конфиг; `fetch-weights` при этом бэкенде ничего не делает).
3. Новый бэкенд: класс-наследник `Predictor` в `predictor.py` + ветка в `build_predictor()`; всё остальное (векторизация,
   склейка, БД) не меняется.
4. После смены весов — `threshold-sweep --split val` (порог привязан к модели), затем `predict --to-db`.

## Дообучение в Colab

`geoqc cv bundle` → загрузить `data/interim/bundle.zip` на Google Drive → открыть `notebooks/train_finetune.ipynb` в Colab
(GPU T4) → указать `BUNDLE_ZIP` → Run all. Архив содержит тайлы, `tiles.gpkg`, разметку val, `tiles_split.csv`, `config/`,
`src/geoqc/**` (ноутбук импортирует пакет через `sys.path`, без `pip install -e`). Подробности и время — `notebooks/README.md`.

## Зависимости от других модулей

- `geoqc.config` (`load_config`, `resolve_path`), `geoqc.db.connect` (только для `--to-db`).
- `geoqc.data.crs.utm_epsg_for` — используется, если доступен, иначе локальная формула зоны UTM (`tiles.utm_epsg_for`).
- `geoqc.eval.detection_metrics` — используется в sweep при наличии функций `match_one_to_one`/`precision_recall_f1`
  (`MatchResult.pairs` — тройки `(gt_idx, pred_idx, iou)`); иначе локальная реализация, поле `metrics_impl` в отчёте.
- Потребители: `matching` (`buildings_det`, `model_version`), `annotation` (`pre-annotate --source detections`), `eval`, `api`.

## Ограничения и известные риски

- Веса `keremberke/yolov8m-building-segmentation`: без тега лицензии в репозитории (наследуют AGPL-3.0 от Ultralytics),
  дообучены автором всего 3 эпохи на Roboflow-датасете 640 px — качество на нашем AOI не гарантировано; ожидаемый путь —
  дообучение в ноутбуке. Fallback `yolov8m-seg.pt` (COCO) зданий не детектирует вовсе.
- Объекты, попавшие на перекрытие, но не задетектированные в одном из тайлов, склейке не подлежат (не с чем);
  дубликаты внутри тайла не объединяются (`merge_same_tile: false`).
- `--to-db` удаляет все прежние `buildings_det` (любой `model_version`) вместе с `matches`/`discrepancies`: в БД одна
  модель, и очередь, экспорт и карта не смешивают версии. Статусы проверки старой очереди теряются — после новой
  детекции очередь строится заново (`make match`). `--append` дописывает тайлы к текущему слою (например, `--split test`
  после `--split val`).
- Тайлы читаются целиком в память батчами (`batch`); при 18 ГБ RAM и MPS `batch: 8` безопасен.
- `sweep --detections <file>` считает метрики по уже склеенным полигонам (фильтр по score без повторной склейки) —
  приближение; точный sweep — без `--detections`.

## Тесты

`pytest tests/cv -q` — 64 теста на синтетике (два перекрывающихся UTM-тайла с яркими квадратами, `MockPredictor`; без сети
и весов; SpaceNet — на поддельном HTTP-сеансе). Маркеры: `db` (загрузка в `buildings_det`), `slow` (U-Net на случайных
весах), `network` (реальный бакет SpaceNet). Статическая проверка отсутствия
ссылок на тестовую разметку — `test_no_test_labels.py`.

## Что нужно знать следующему агенту

- `model_version` в `detections.geojson` и `buildings_det` должен совпадать с `config/cv.yaml` — после любой смены весов
  запускайте `fetch-weights` (или ячейку экспорта ноутбука), затем `predict`.
- `threshold-sweep` без `--holdout` использует **всю** val-разметку, включая тайлы, на которых модель дообучалась; после
  дообучения используйте `--holdout`.
- `export-dataset` считает здание на перекрытии тайлов в обоих тайлах (это корректно для обучения); `split.csv` фиксирует
  train/holdout, `read_holdout_tile_ids()` — единственный источник holdout для sweep.
- `pipeline.run_predict(..., keep_raw=True)` возвращает `raw` — детекции по тайлам до склейки (для анализа ошибок).
