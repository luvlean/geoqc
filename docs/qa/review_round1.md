# QA — ревью, раунд 1 (T8)

Дата: 2026-09-26. Проверяющий: QA (выполнено координатором инлайн — субагенты недоступны по квоте).

## Что добавлено

| Файл | Что проверяет |
|---|---|
| `tests/integration/test_constraints.py` | нет `labels/test` в cv/matching/notebooks; нет URL тайлов Esri/Bing/Google/Yandex в data/cv/notebooks; `.env` не в git; нет непустых ключей в отслеживаемых файлах; `sql/001` = CONTRACTS §3.7 |
| `tests/integration/test_cli.py` | `geoqc --help` и `--help` каждого из 6 саб-приложений |
| `tests/integration/test_pipeline_synthetic.py` (`db`) | mock-детекция на 2 синтетических тайлах → `buildings_det` → `matching run` → ожидаемые классы (1 match, по одному `missing_in_map`, `missing_on_image`, `geometry_mismatch`) → `eval detection` (TP=3, F1=1) → `build_metrics`: схема §3.9, G1–G9, отсутствующие входы остаются `null` |
| `tests/data/test_georef_preprocess.py` | G1: `offset_for_building` восстанавливает известный сдвиг (±0,3 м, знаки восток/север), nodata-окно отбрасывается; отбор контрольных зданий; `build_aoi_cog`: сетка кратна GSD, `--shift` сдвигает содержимое в нужную сторону, вне AOI — nodata |

## Находки

| Severity | Модуль | Файл:строка | Описание | Предложение | Статус |
|---|---|---|---|---|---|
| major | data | `src/geoqc/data/cli.py` `_raw_path` | `raw_path` из `config/data.yaml` применялся к любому снимку: после смены AOI качался новый снимок в файл старого | брать `raw_path` только при совпадении `image.id` | исправлено, `tests/data/test_raw_path.py` |
| major | data | `src/geoqc/data/{preprocess,georef}.py` | 0 % покрытия; знаки сдвига G1 не проверялись | офлайн-тесты на синтетике | исправлено (см. выше) |
| major | cv | `models/MODEL_CARD.md` | у HF-чекпоинта нет лицензии, веса — производная AGPL-3.0 (ultralytics) | для коммерческой передачи: лицензия Ultralytics Enterprise или U-Net из ноутбука (без AGPL) | открыто, решение за владельцем |
| minor | web | `web/app.js` `osmLink` | ссылка всегда `way/<id>`; relation хранятся с отрицательным `osm_id` → битая ссылка | `id < 0` → `relation/<-id>` | исправлено, README API обновлён |
| minor | tests | `tests/matching/test_cli.py`, `tests/annotation/test_qgis_project.py` | тесты читали реальные `config/aoi.geojson` / `data/interim/aoi_cog.tif` и падали после `make data` | изолировать от рабочих данных | исправлено |
| minor | infra | `Makefile` `labels-prep`/`labels-validate` | `qgis-project` без `--split`; нет `finalize` перед `validate` | вызовы по сплитам | исправлено |
| minor | data | `src/geoqc/data/preprocess.py:34` | `data preprocess` переписывает `crs.metric` в отслеживаемом `config/pipeline.yaml` | ожидаемо (UTM зависит от AOI); закоммитить вместе с `config/aoi.geojson` | принято |
| minor | data | `src/geoqc/data/{download,secondary,aoi}.py` | покрыты только `network`-тестами (`GEOQC_NETWORK=1`) | записанные ответы (vcrpy) — вне MVP | открыто |
| minor | data | `src/geoqc/data/manifest.py`, `georef.py`, `annotation/common.py` | в отслеживаемые JSON (`manifest.json`, `tiles.gpkg`, `reports/georef.json`, сводки разметки) писались абсолютные пути `/Users/...` | `config.repo_relative` при записи; `qgis/layers.json` (нужны абсолютные пути для QGIS) — в `.gitignore` | исправлено, `tests/data/test_manifest_paths.py` |
| minor | tests | `tests/eval/conftest.py` `synthetic_env` | CLI-тесты eval брали реальные `data/manifest.json` (G1 passed) → ожидание «все гейты не пройдены» ломалось | `paths.*` синтетического `pipeline.yaml` → tmp | исправлено |

SQL: динамические фрагменты только константные (`matcher.stats`), значения — параметрами; в API — `psycopg.sql.Composable`. Инъекций не найдено. Секретов в git нет; ключ Esri только на сервере (D-002).

## Прогон

- `ruff format --check . && ruff check .` — чисто; `make notebooks-check`, `make compose-config` — OK.
- `make test-cov` — зелёный (итоговое число — в `LOG.md`), покрытие 69 % (data 28 % → выше после новых тестов; остальные модули 77–92 %).

## Интеграционный прогон на реальных данных

Числа — диагностика конвейера, **не метрики работы** (разметка val не верифицирована, test не открывался).

- Data: G1 пройден — медианный сдвиг 0,487 м, n = 40 контрольных зданий; 354 тайла.
- CV (HF-чекпоинт, без дообучения): 1646 детекций в `buildings_det`.
- Matching: 1008 пар (средний IoU 0,781), 1260 расхождений; баланс по OSM в AOI сходится: 1516 = 1008 + 136 + 372.
- Eval smoke на черновиках val: F1 ≈ 0,50 → базовая модель, вероятно, не пройдёт G3; нужен ноутбук дообучения.
- OSM — снимок зеркала на 2026-07-24, снимок — 2021-07-26 (D-014).

## G8 / G9

- **G9** (pytest) — выполнен.
- **G8** (`make all` в чистом окружении) — не проверялся целиком: `make data` идёт на реальных данных, дальше `labels-prep` → правка в QGIS и `verified=true` (человек), обучение в ноутбуке (человек), `reports/verification.csv` ≥ 50 строк (человек). Без этих шагов `eval` даёт только smoke-отчёт.

## Не проверялось

- `docker build` в этом раунде (образ собран и проверен в T1).
- Обучение в ноутбуке — запускает пользователь.
- Метрики на test — одноразовый прогон после верификации разметки.
