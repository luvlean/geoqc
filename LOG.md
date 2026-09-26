# LOG.md — журнал работы агентов

Формат блока — `docs/CONTRACTS.md §10`. Новые записи добавляются в конец.

## [2026-09-26] Координатор — T0 Инициализация
- Сделано: структура репозитория, `.venv` (Python 3.11), `requirements*.txt`, `pyproject.toml`, `sql/001_schema.sql`, `docker-compose.yml` (db), `config/pipeline.yaml`, `config/quality_gates.yaml`, `src/geoqc/{config,db,cli}.py`, `tests/conftest.py`, `docs/CONTRACTS.md`, `docs/tasks/*.md`, `PLAN.md`, `DECISIONS.md`.
- Решения: D-001 … D-010.
- Не получилось / риски: покрытие открытыми снимками ограничено, AOI будет за рубежом.
- Команды проверки: `.venv/bin/python -c "import geoqc"`; `docker compose up -d db`.
