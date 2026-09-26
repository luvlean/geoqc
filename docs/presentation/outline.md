# Презентация к защите — сценарий (11 слайдов)

Формат: 10–12 слайдов, 7–8 минут. Каждое число на слайде берётся из указанного файла/ключа; до появления файла — плейсхолдер `{{…}}`. Скриншоты — только с атрибуцией источников (OSM ODbL, снимок по `data/manifest.json`; Esri — только подложка, D-002).

| № | Слайд | Что показать | Откуда цифры / материалы |
|---|---|---|---|
| 1 | Титул | Тема, автор (Жуков Д.А., БФБО-01-24), руководитель, формат «оркестр ИИ-агентов + human-in-the-loop» | — |
| 2 | Проблема | Карта устаревает: снесённые/новые здания, смещённые контуры; ручная сверка со снимком не масштабируется. Один скриншот: снимок vs контуры OSM с явным расхождением | `reports/figures/` (пример из `error-analysis`) или скриншот веб-карты; площадь AOI `{{manifest.aoi.area_km2}}` км², зданий OSM `{{manifest.osm.count}}` (`data/manifest.json`) |
| 3 | Решение и ценность | Очередь расхождений с классом и `confidence`; пользователь смотрит только кандидатов. Три класса: `missing_in_map`, `missing_on_image`, `geometry_mismatch`. Метрика ценности — precision@N. Автоправка OSM не делается | `docs/CONTRACTS.md` §3.7; определения — `docs/metrics_and_formulas.md` §5, §8 |
| 4 | Архитектура | Mermaid-схема из `README.md`: data → cv → matching → eval / api. Стек: Python 3.11, rasterio/shapely, PyTorch + YOLOv8-seg/U-Net, PostGIS, FastAPI, Leaflet, Docker | `README.md`, `docs/architecture.md` §1–§2 |
| 5 | Данные и лицензии | Источник снимка, дата, GSD, лицензия; срез OSM (дата, ODbL); второй источник; проверка геопривязки | `data/manifest.json → image.{source,acquired,gsd_m,license}`, `osm.snapshot_date`, `secondary.source`; G1: `{{metrics.georeference.median_offset_m}}` м на `{{metrics.georeference.n_control_points}}` точках (`reports/metrics.json`) |
| 6 | Эталонная разметка | Почему OSM не ground truth; предразметка + правка в QGIS; val/test без пересечения тайлов; слепая повторная разметка 10 % | `{{metrics.dataset.n_tiles_val}}` / `{{metrics.dataset.n_tiles_test}}` тайлов, `{{metrics.dataset.n_gt_val}}` / `{{metrics.dataset.n_gt_test}}` зданий; G2: `{{metrics.annotation.agreement_iou}}` (`reports/metrics.json`); протокол `docs/annotation/PROTOCOL.md` |
| 7 | Детекция и векторизация | Модель (`model_version`), порог подобран на val; маска → полигоны → склейка на перекрытии → DP-упрощение → фильтр площади. Один пример тайл+GT+pred | `{{metrics.model.model_version}}`, `{{metrics.model.conf_threshold}}`; кривая порога — `reports/threshold_sweep_val.json`; рисунок — `reports/figures/`; `models/MODEL_CARD.md` |
| 8 | Метрики | Таблица: P/R/F1/mean IoU на val и test; статус гейтов G3–G5; сравнение с вторым источником. Честно показать непройденные гейты с причиной | `{{metrics.detection.val.f1}}`, `{{metrics.detection.test.f1}}`, `{{metrics.detection.test.precision}}`, `{{metrics.detection.test.recall}}`, `{{metrics.detection.test.mean_iou_matched}}`; `{{metrics.comparison.detection_vs_secondary.f1}}`; `{{metrics.gates.G3.passed}}` … (`reports/metrics.json`); таблица — `scripts/render_metrics_table.py table` |
| 9 | Очередь расхождений и верификация | Число расхождений по классам; ручная проверка ≥ 50 шт.; precision по классам, overall, precision@20 (G6, G7). Формула confidence одной строкой | `{{metrics.discrepancies.counts_by_class.missing_in_map}}` и т. д.; `{{metrics.discrepancies.verified_by_class.<class>.precision}}`, `{{metrics.discrepancies.precision_overall}}`, `{{metrics.discrepancies.precision_at_n.value}}` (n = `{{metrics.discrepancies.precision_at_n.n}}`); `reports/verification.csv`; формула — `src/geoqc/matching/README.md` |
| 10 | Демо | Скриншот/видео веб-карты: фильтр класса → список топ-N → карточка (класс, confidence, IoU, ссылка osm.org) → «Принять/Отклонить» → экспорт GeoJSON. Команда запуска `make demo` | `web/`, `src/geoqc/api/README.md` (примеры `curl`); время ответа API — `LOG.md` (App Agent) |
| 11 | Ограничения и развитие | Ограничения: разновременность снимка и OSM, крыши vs фундаменты (off-nadir, тени, деревья), один снимок/один срез, зависимость G6/G7 от объёма ручной проверки. Развитие: мониторинг во времени, другие классы (дороги), масштабирование на город, интеграция с MapRoulette. Итог: воспроизводимость `make all` (G8), тесты (G9) | `README.md` «Ограничения»; `{{metrics.reproducibility.make_all_passed}}`, `{{metrics.tests.passed}}`, `{{metrics.tests.n_tests}}`; `reports/error_analysis.md` — типовые провалы |

Опциональный 12-й слайд — «Оркестр агентов»: матрица владения (CONTRACTS §1), контрольные точки студента, журнал решений `DECISIONS.md` (D-001…D-010), число задач/агентов из `PLAN.md`.

## Подготовка

1. После финального `geoqc eval gates`: `python scripts/render_metrics_table.py table` → таблицы для слайдов 8–9.
2. Проверить, что все `{{…}}` заменены (`fill` возвращает 0).
3. Рисунки — только из `reports/figures/` и скриншоты веб-карты с атрибуцией.
4. Ответы на ожидаемые вопросы: почему OSM не эталон (D-005); почему Esri только подложка (D-002); как гарантирована изоляция `labels/test` (статический тест QA); что делать при непройденном гейте (plan §6, §8 — fallback).
