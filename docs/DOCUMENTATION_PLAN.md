# План ведения документации geoqc

Цель: документация точная, без выдуманных цифр, воспроизводимая из артефактов `reports/`. Ниже — перечень документов, владельцы, триггеры обновления, правила для чисел, формул и рисунков, генерация таблиц метрик.

## 1. Инвентарь документов

| Документ | Владелец | Назначение | Обновлять когда |
|---|---|---|---|
| `README.md` | Docs | вход в проект: назначение, архитектура, быстрый старт, лицензии, ограничения | изменился `Makefile`, CLI (§6 контрактов), лицензия источника, добавлен модуль |
| `docs/CONTRACTS.md` | Координатор | контракты данных/CLI/API/метрик | только через `DECISIONS.md` |
| `docs/tasks/*.md` | Координатор | ТЗ агентов | постановка/пересмотр задачи |
| `docs/architecture.md` | Docs | компоненты, потоки, CRS, БД, make-таргеты | новая таблица/функция SQL, новый таргет, изменение CRS-политики |
| `docs/metrics_and_formulas.md` | Docs (формулы согласуются с Eval/Geo) | все формулы с обозначениями и примерами, таблица гейтов | изменение формулы в `eval`/`matching`/`cv`, изменение `quality_gates.yaml` |
| `docs/DOCUMENTATION_PLAN.md` | Docs | этот документ | добавлен/удалён документ, изменены правила |
| `src/geoqc/<module>/README.md` (6 шт.) | владелец модуля | назначение, входы/выходы, команды с примерами, параметры `config/<module>.yaml`, формулы, зависимости, ограничения, что должен знать следующий агент (`docs/tasks/00_common.md` п.7) | любое изменение CLI, конфига, формулы, ограничения |
| `docs/annotation/PROTOCOL.md`, `INSTRUCTION_QGIS.md`, `data/labels/README.md` | Annotation | протокол разметки, инструкция студента, статусы каталога | изменение правил разметки, полей GeoJSON, команд `validate` |
| `notebooks/README.md` | CV | запуск ноутбуков, время, действия после обучения | изменение ноутбука |
| `models/MODEL_CARD.md` | CV | источник весов, лицензия, данные дообучения, гиперпараметры, `model_version`, порог и сплит подбора | каждое новое обучение / смена весов |
| `reports/error_analysis.md`, `reports/figures/` | Eval (генерируется кодом) | анализ ошибок с цифрами и примерами | каждый `geoqc eval error-analysis` |
| `docs/qa/review_round*.md` | QA | находки ревью, итоги G8/G9, что не проверялось | каждый раунд ревью |
| `PLAN.md`, `DECISIONS.md`, `LOG.md` | Координатор (LOG — все агенты, append-only) | план, решения D-NNN, журнал работ | по ходу проекта |
| `docs/thesis/*.md` | Docs + студент | пояснительная записка (§11 плана) | после финального `reports/metrics.json`; правки текста студентом |
| `docs/presentation/outline.md` | Docs + студент | сценарий 10–12 слайдов | то же |
| `CONTRIBUTING.md`, `LICENSE` | Orchestrator | правила работы, лицензия кода | изменение процесса |

Правило владения: чужие документы не правят — находки передаются координатору (LOG / отчёт QA).

## 2. Чек-лист «каждая цифра → путь в `reports/` → ключ json»

Любое число в документации (кроме иллюстративных примеров формул, явно помеченных как синтетические, и порогов из `config/quality_gates.yaml`) должно проходить проверку:

1. Число порождено кодом репозитория (модуль `eval`, `data check-georef`, `annotation validate`, `matching stats`), не введено вручную.
2. Оно лежит в файле `reports/…` в git.
3. Указан точный ключ: например, «F1 на test — `reports/metrics.json → detection.test.f1`».
4. В черновиках (записка, презентация) до появления файла стоит плейсхолдер `{{metrics.<путь.к.ключу>}}`, а не число.
5. При обновлении `reports/metrics.json` все документы с цифрами перегенерируются (см. §5), а не правятся вручную.
6. Пороги гейтов цитируются из `config/quality_gates.yaml` со ссылкой; фактические значения — из `metrics.json → gates.<G>.actual`.
7. Числа про данные (площадь AOI, число тайлов, число зданий OSM, дата снимка) — из `data/manifest.json` (`aoi.area_km2`, `osm.count`, `image.acquired`), число тайлов сплитов — `metrics.json → dataset.n_tiles_val/n_tiles_test`, разметка — `dataset.n_gt_val/n_gt_test`.
8. Число детекций и расхождений по классам — `metrics.json → discrepancies.counts_by_class` или вывод `geoqc matching stats`, зафиксированный в `LOG.md`.

Таблица соответствия для черновиков:

| Утверждение | Источник | Ключ |
|---|---|---|
| смещение геопривязки | `reports/metrics.json` | `georeference.median_offset_m`, `georeference.n_control_points` |
| согласие разметки | `reports/metrics.json` | `annotation.agreement_iou`, `annotation.agreement_f1`, `annotation.n_checked` |
| P/R/F1, mean IoU по сплитам | `reports/metrics.json` | `detection.{val,test}.*` |
| число расхождений по классам | `reports/metrics.json` | `discrepancies.counts_by_class.*` |
| precision по классам, overall, @N | `reports/metrics.json` | `discrepancies.verified_by_class.*`, `precision_overall`, `precision_at_n` |
| сравнение с вторым источником | `reports/metrics.json` | `comparison.detection_vs_secondary.*` |
| статус гейтов | `reports/metrics.json` | `gates.<G>.{actual,passed,note}` |
| порог уверенности и `model_version` | `reports/metrics.json` | `model.conf_threshold`, `model.model_version` |
| кривая подбора порога | `reports/threshold_sweep_val.json` | по схеме модуля `cv` |
| анализ ошибок по стратам/тайлам | `reports/error_analysis.md`, `reports/figures/*.png` | — |
| время ответа API | `LOG.md` (App Agent) | — |

## 3. Правила записи формул

- Формат: Markdown + LaTeX (`$…$`, `$$…$$`); одна формула — один смысл, все символы введены в таблице обозначений `docs/metrics_and_formulas.md` §0.
- Единицы указываются всегда: площади — м², расстояния — м, IoU/precision/recall/F1/confidence — безразмерные в $[0,1]$.
- CRS указывается для каждой метрической величины: расчёт — метрическая UTM-зона AOI (`config/pipeline.yaml → crs.metric`); хранение — EPSG:4326. Формула без указания CRS считается неполной.
- Пороги в формулах — имена параметров конфига (`iou_match`, `min_area_m2`), а не только числа.
- Каждая формула имеет синтетический пример на числах с явной пометкой «пример, не результат»; предпочтительны примеры, воспроизведённые в тестах (`tests/eval`, `tests/matching`: квадраты 10×10, сдвиг 5 м → IoU 1/3).
- Формула в документации совпадает с реализацией: при расхождении прав код или документ, и это фиксируется в LOG. README модуля — первичный источник формулы модуля; `docs/metrics_and_formulas.md` — сводка со ссылками.
- Соглашения о крайних случаях (деление на ноль, пустые множества) записываются рядом с формулой.

## 4. Правила рисунков и скриншотов

- Рисунки с данными (гистограммы IoU, примеры тайл+GT+pred, кривые порога) — только сгенерированные кодом (`geoqc eval error-analysis`) в `reports/figures/`, имена стабильные: `reports/figures/<тема>_<split>.png`. Ручные правки PNG запрещены.
- Каждый рисунок в записке/презентации: подпись, источник (путь к файлу), команда генерации, сплит, `model_version`.
- Скриншоты веб-карты и QGIS — с атрибуцией источников в кадре (OSM ODbL, снимок по манифесту, Esri — если включена подложка, только как подложка, D-002). Без ключей, токенов и персональных данных в кадре.
- Диаграммы архитектуры — mermaid в Markdown (правится текстом, версионируется); экспорт в PNG — только для презентации.
- Цвета классов расхождений едины во всех рисунках и в `web/`: фиксируются в README `api` и переиспользуются в `error_analysis`.
- Не показывать `data/labels/test/` на рисунках, используемых при разработке `cv` (правило разделения обучения и оценки).

## 5. Генерация таблиц метрик из `reports/metrics.json`

Скрипт `scripts/render_metrics_table.py`. Два режима: `table` печатает Markdown-таблицы гейтов и детекции; `fill` подставляет `{{metrics.<path>}}` в документах.

```bash
python scripts/render_metrics_table.py table                                   # таблицы детекции и гейтов
python scripts/render_metrics_table.py fill docs/thesis/*.md --out-dir build/thesis
```

Правила использования: исходники `docs/thesis/*.md` хранят плейсхолдеры и не перезаписываются; заполненные копии — в `build/` (не в git) или подставляются при вёрстке. Ненулевой код возврата при отсутствующих ключах блокирует сборку записки с «дырами».

## 6. Процедура обновления после интеграции

1. Eval Agent: `geoqc eval gates` → `reports/metrics.json` (+ `error_analysis.md`, `figures/`).
2. Docs Agent: `python scripts/render_metrics_table.py table` → сверить таблицу гейтов с текстом README/записки; `fill` → проверить отсутствие `missing key`.
3. Сверка формул: `docs/metrics_and_formulas.md` §13 против README модулей `matching`, `eval`, `annotation`, `data`, `cv`.
4. Сверка команд: `make help`, `geoqc <module> --help` против README и `docs/architecture.md` §5.
5. Сверка лицензий: `data/manifest.json → image.license/attribution, osm.license, secondary.license`, `models/MODEL_CARD.md` против README «Лицензии и атрибуция».
6. Записать блок в `LOG.md`; расхождения кода и документации — координатору.

## 7. Стиль

Русский, лаконично, без вводных слов; термины кода, ключи, пути и команды — в обратных кавычках; английские имена классов расхождений не переводятся; формулы — LaTeX; таблицы предпочтительнее списков для соответствий «что → где».
