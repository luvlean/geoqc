# Глава 5. Программная реализация

## 5.1 Стек

Python 3.11; GDAL/rasterio, rio-cogeo, shapely 2, geopandas/pyogrio, pyproj — геоданные; PyTorch, Ultralytics YOLOv8-seg — детекция; PostgreSQL + PostGIS — хранение и пространственные операции; FastAPI + uvicorn — API; Leaflet — веб-карта; Docker Compose — окружение; pytest, ruff, GitHub Actions — качество. Лицензия кода — MIT.

## 5.2 Структура пакета

Единый устанавливаемый пакет `geoqc` (`src/geoqc/`, D-007) с модулями `data`, `annotation`, `cv`, `matching`, `eval`, `api` и общими компонентами: `config.py` (слияние `config/pipeline.yaml` и `config/<module>.yaml`, секреты из `.env` через pydantic-settings), `db.py` (psycopg 3, `apply_schema` применяет `sql/NNN_*.sql` по порядку), `cli.py` (typer; каждый модуль регистрирует свой `cli.py` как саб-приложение: `geoqc <module> <command>`). Конфигурация разнесена по модулям (D-009), чтобы владельцы не конфликтовали при параллельной работе.

## 5.3 База данных

Схема `sql/001_schema.sql` — контракт (Приложение А): `buildings_ref` (эталон OSM и вторичные источники, `source`), `buildings_det` (детекции, `score`, `model_version`, `tile_id`), `matches` (пары IoU ≥ 0.5, уникальность по `ref_id`/`det_id` в рамках `model_version` обеспечивает 1:1), `discrepancies` (класс, `confidence`, `status ∈ {new, confirmed, rejected}`, ссылки на стороны, `updated_at`). Геометрии — `MultiPolygon, 4326` с GiST-индексами; под запросы API — индексы `(class, confidence DESC)` и `status`. Функции сопоставления — `sql/002_matching.sql` (`geoqc_iou`, `geoqc_candidate_pairs`, `geoqc_match_greedy`), метрические вычисления через `ST_Transform` в UTM-зону. Все записи идемпотентны по `model_version`/`source`. SQL параметризован — конкатенация пользовательского ввода запрещена и проверяется ревью.

## 5.4 CLI и воспроизводимость

Обязательные команды каждого модуля зафиксированы контрактом (`docs/CONTRACTS.md` §6); `Makefile` оборачивает их в таргеты `install up down db-init data labels-prep detect match eval demo all test lint ci clean`, где `all = up db-init data detect match eval`. Требование гейта G8 — `make all` в чистом окружении без ручных шагов, кроме явно выделенных контрольных точек человека (разметка, верификация, дообучение). Окружение с нуля — `scripts/bootstrap.sh`; секреты — `.env` по `.env.example`.

Детерминизм: `random_seed` в конфиге, фиксированные сплиты, `model_version` из хэша весов; однократность оценки на `test` — `geoqc eval detection --split test` отказывается перезаписывать результат без `--force` и ведёт `reports/metrics_test.runs.log`.

## 5.5 API

FastAPI (`src/geoqc/api`), пул соединений psycopg, статика `web/` по `/`:

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health`, `/stats`, `/config/map` | статус, счётчики по классам/статусам, центр/bbox AOI, атрибуции, `esri_enabled` |
| GET | `/discrepancies?bbox&class&min_conf&status&limit&offset` | очередь GeoJSON, сортировка `confidence DESC` |
| GET / PATCH | `/discrepancies/{id}` | карточка; смена статуса `{"status": "confirmed"|"rejected"|"new"}` |
| GET | `/tiles/{z}/{x}/{y}.mvt` | векторные тайлы (`ST_AsMVT`) слоёв `discrepancies`, `buildings_ref`, `buildings_det` |
| GET | `/imagery/{z}/{x}/{y}.png` | тайлы снимка из COG (rio-tiler) |
| GET | `/export?class&min_conf&status` | очередь в формате контракта §3.8 (MapRoulette / JOSM) |

Ключ Esri не передаётся на клиент: подложка проксируется сервером с подстановкой токена (D-002). Требование к отклику: запрос по bbox всей AOI < 1 с (замер — `LOG.md`, App Agent).

## 5.6 Веб-интерфейс

Leaflet без сборщика (`web/index.html`, `app.js`, `style.css`): подложки OSM / снимок / Esri (если включена); слои OSM-зданий, детекций и расхождений с цветом по классу; панель фильтров (класс, минимальная уверенность, статус); список топ-N по уверенности с переходом к объекту; карточка (класс, `confidence`, IoU, `osm_id` со ссылкой на osm.org); кнопки «Принять / Отклонить» → `PATCH`; «Экспорт GeoJSON»; атрибуция всех источников в углу карты. Сценарий валидатора: зона → список → карточка → статус → экспорт.

## 5.7 Docker и CI

`docker-compose.yml`: сервис `db` (`postgis/postgis`, порт 5433 — D-010; на первом старте создаётся тестовая БД `geoqc_test`), сервис `app` (образ из `Dockerfile` на `python:3.11-slim`, non-root, uvicorn на 8000, данные и статика read-only), профиль `jupyter`. CI — GitHub Actions (D-003): `lint` (ruff), `test` (pytest с сервисом PostGIS, маркеры `db/network/slow/gpu`), `docker` (сборка и `compose config`), `notebooks` (nbqa ruff, `nbstripout --verify`).

## 5.8 Тестирование

`tests/<module>/` — модульные тесты на синтетических геометриях (квадраты 10×10 м, сдвиг 5 м → IoU 1/3; конфликтные назначения; крайние случаи P/R/F1; идемпотентность записи в БД); `tests/integration/` — сквозной синтетический прогон (растр 2×2 тайла → разметка → мок-детекции → БД → сопоставление → метрики → гейты), проверка `--help` всех саб-приложений, статические ограничения (нет обращений к `labels/test` в `cv`/ноутбуках/`matching`, нет секретов и URL закрытых тайлов в коде, схема `001_schema.sql` не изменена). Гейт G9 — pytest проходит, сопоставление и классификация покрыты: `{{metrics.tests.passed}}`, тестов `{{metrics.tests.n_tests}}`.

## 5.9 Организация разработки: оркестр агентов

Девять ролей (Orchestrator, Data, Annotation, CV, Geo, Eval, App, QA, Docs) работали параллельно в одном рабочем дереве (D-006) с матрицей владения путей (`docs/CONTRACTS.md` §1): агент пишет только в свои директории, контракты меняются лишь через журнал решений `DECISIONS.md`, каждый агент завершает задачу блоком в `LOG.md`. Разделение обучения и оценки закреплено структурно: `data/labels/test/` доступен только Annotation, Eval и QA. Приёмка — по DoD и командам `make`/`geoqc`, а не «работает у меня». Студент — контрольные точки: разметка в QGIS, запуск ноутбука, верификация ≥ 50 расхождений, финальный прогон демо.
