# ТЗ T1 — Orchestrator Agent: инфраструктура, воспроизводимость, CI

**Цель.** Проект собирается и проверяется одной командой локально, в Docker и в GitHub Actions.

**Входы.** `docs/CONTRACTS.md` §6–§7 (CLI и make-таргеты), `docker-compose.yml` (db уже есть), `pyproject.toml`, `requirements*.txt`.

**Выходы.**
- `Makefile` (GNU make 3.81): `install up down db-init data labels-prep detect match eval demo all test lint bundle ci clean`, переменные `PY=.venv/bin/python`, `GEOQC=.venv/bin/geoqc`; каждый таргет — соответствующая CLI-команда из §6; `help` по умолчанию с описанием таргетов.
- `Dockerfile` — `python:3.11-slim`, системные библиотеки для rasterio/opencv (libgl1, libglib2.0-0, libexpat1), установка `requirements.txt`, `pip install -e .`, non-root user, `CMD uvicorn geoqc.api.main:app --host 0.0.0.0 --port 8000`.
- `docker-compose.yml`: добавить сервис `app` (build ., порты 8000, `depends_on: db: condition: service_healthy`, env из `.env`, volume `./data:/app/data:ro`, `./web:/app/web:ro`), профиль `jupyter` (сервис jupyterlab на 8888, optional). Сервис `db` не менять.
- `.github/workflows/ci.yml`: триггеры push/PR; jobs: `lint` (ruff format --check, ruff check), `test` (Python 3.11, сервис `postgis/postgis:17-3.6-alpine`, создать `geoqc_test`, `TEST_DATABASE_URL`, `pytest -m "not network and not slow and not gpu" --cov=geoqc --cov-report=xml`), `docker` (docker build, `docker compose config`), `notebooks` (`nbqa ruff notebooks/`, проверка что ноутбуки без outputs через `nbstripout --verify`). Кэш pip. Concurrency на ветку.
- `.pre-commit-config.yaml` (ruff, ruff-format, nbstripout, end-of-file-fixer, check-yaml).
- `scripts/ci/wait_for_db.sh`, `scripts/ci/create_test_db.sh`; `scripts/bootstrap.sh` — установка окружения с нуля (venv, deps, `pip install -e .`, `.env` из example).
- `CONTRIBUTING.md` (кратко: ветки, коммиты на английском в одну строку, ruff, тесты, LOG), `LICENSE` (MIT), `.editorconfig`, `.dockerignore`.

**Проверки (DoD).**
- `make help`, `make lint`, `make up`, `make db-init` (когда Geo сдаст `init-db`; до этого — применить `sql/001_schema.sql` через `geoqc.db.apply_schema` в fallback-таргете) выполняются без ошибок на этой машине.
- `docker build .` проходит (если сборка >10 мин — запустить в фоне и приложить лог).
- `act` не требуется; workflow валидируется `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`.
- Все make-таргеты ссылаются только на команды §6 контрактов, даже если модуль ещё не сдан.

**LOG:** какие таргеты проверены реально, какие ждут модулей.
