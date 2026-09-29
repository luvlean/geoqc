# Модуль `geoqc.eval` — метрики, верификация, гейты

## Назначение

Считает все числа, которые попадают в отчёт и диплом: качество детекции на `val` и `test`,
точность очереди расхождений по ручной верификации, анализ ошибок и итоговый `reports/metrics.json` с гейтами G1–G9.
Модуль ничего не обучает и не меняет данные.

Формулы — в [docs/metrics_and_formulas.md](../../../docs/metrics_and_formulas.md): §1–4 детекция, §7–8 precision и Precision@N, §12 гейты.

## Команды

```bash
geoqc eval detection --split val|test [--min-score S] [--method greedy|hungarian] [--allow-unverified]
                                                   # → reports/metrics_<split>.json
geoqc eval verification-sample [--model-version V] # → reports/verification_sample.csv
geoqc eval discrepancies --verification reports/verification.csv
                                                   # → reports/metrics_discrepancies.json
geoqc eval error-analysis [--split val]            # → reports/error_analysis.md, .json, figures/
geoqc eval compare-secondary [--split test]        # → reports/comparison.json
geoqc eval gates [--make-all-passed] [--tests-passed --n-tests N]
                                                   # → reports/metrics.json
```

Make-цель `eval` вызывает их по порядку. `discrepancies` запускается, только если есть `reports/verification.csv`;
`detection --split test` пропускается, если `reports/metrics_test.json` уже есть, поэтому `make eval` можно повторять
(например, после заполнения `verification.csv`). Если метрики val/test посчитаны не той `model_version`, что в
`config/cv.yaml`, `gates` пишет это в `note` гейтов G3–G5.

## Детекция

1. Эталон — `data/labels/<split>/buildings.geojson`, только тайлы сплита из `tiles_split.csv`.
2. Детекции обрезаются по объединению тайлов сплита. Обрезки меньше `min_area_m2` отбрасываются, чтобы край тайла не давал ложных FP.
3. Сопоставление 1:1 в метрической CRS: жадное по убыванию IoU или венгерское. Пара — TP при IoU ≥ `iou_threshold` (0.5).
4. `P = TP/(TP+FP)`, `R = TP/(TP+FN)`, `F1 = 2PR/(P+R)`, `mean_iou_matched` — среднее IoU по TP-парам.
5. Отчёт содержит итог, разбивку по тайлам и по стратам.

Эталон с `verified=false` отклоняется. С флагом `--allow-unverified` получается дымовой прогон:
файл `metrics_<split>_smoke.json` помечен как не метрика и в `metrics.json` не попадает.

**Тест оценивается один раз.** Повторный `--split test` отклоняется с кодом 3.
Перезапуск возможен только с `--force --reason "<почему>"`. Каждая попытка пишется в `reports/metrics_test.runs.log`.

## Второй источник

`compare-secondary` оценивает против разметки test и детекции, и футпринты Microsoft/Google как альтернативный детектор.
Черновики разметки строятся из тех же футпринтов, поэтому неправленый черновик завышает `secondary_vs_gt`: полигоны
разметки с IoU ≥ 0.98 к футпринту считаются в `n_gt_copied_from_secondary`, при ненулевом значении в `notes` пишется
предупреждение.

## Верификация расхождений

`verification-sample` строит выборку для ручной проверки, не меньше `sample_size` (50):

1. `top_k_global` (20) самых уверенных расхождений всей очереди. Они же дают Precision@20.
2. Добор до `per_class_min` (15) в каждом классе: половина дефицита — следующие по уверенности, остальное — случайно с зерном `seed`.
3. Случайный добор до `sample_size`.

Студент заполняет колонку `verdict` и сохраняет файл как `reports/verification.csv`:

| verdict | Смысл |
|---|---|
| `map_error` | расхождение реальное: ошибка в OSM |
| `model_error` | ошибка детектора |
| `image_issue` | проблема снимка: тень, облако, смещение, дата съёмки |
| `unsure` | нельзя решить по снимку |

`precision = map_error / (n − unsure)` для каждого класса и в целом.
Precision@N ранжирует проверенные строки по `confidence` и применяет ту же формулу к первым N.
`value_incl_unsure = map_error / N` показан рядом для честности.

`discrepancies` сверяет `id` проверенных строк с текущей очередью. После `make detect`/`make match` id расхождений
меняются: старые вердикты попадают в `n_not_in_queue`, в `notes` и в `note` гейтов G6/G7. Нужна новая выборка и проверка.

## Анализ ошибок

`error-analysis` раскладывает FP и FN по тайлам, стратам, площади и IoU и помечает типичные причины:

| Признак | Правило |
|---|---|
| у края тайла | ближе `tile_border_buffer_m` к границе тайла |
| сложная крыша | площадь / площадь выпуклой оболочки < `convexity_threshold` |
| в тени | яркость FN ниже квантиля `shadow_luminance_quantile` яркости TP |
| растительность | средний ExG = 2G − R − B выше `vegetation_exg_threshold` |

Худшие тайлы рисуются в `reports/figures/`.

## Гейты

`gates` собирает `metrics.json` по схеме `docs/CONTRACTS.md` §3.9 из промежуточных отчётов:
`georef.json`, `annotation.json`, `metrics_val.json`, `metrics_test.json`, `metrics_discrepancies.json`.
Пороги — `config/quality_gates.yaml`. Отсутствующий вход даёт `actual: null` и `passed: false`, а не выдуманное число.
G8 и G9 передаются флагами, потому что их подтверждают внешние прогоны `make all` и `pytest`.

## Правила для агентов

- Числа в документах берутся только из `reports/*.json`, руками не переписываются.
- `test` не используется для подбора порогов: `threshold-sweep` в CV и настройки здесь работают только на `val`.
- Метрики детекции считаются по `data/interim/detections.geojson`, где дубли на стыках тайлов уже склеены CV.
  Дополнительное подавление дублей в базе (`det_suppressed`, D-012) влияет только на очередь расхождений.
- Тесты — `tests/eval/`, синтетика с известными ответами.
