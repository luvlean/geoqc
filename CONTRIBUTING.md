# Как вносить изменения в geoqc

Коротко о правилах работы в репозитории. Подробности — в `docs/CONTRACTS.md` (зоны ответственности §1, CLI §6, make-таргеты §7) и `DECISIONS.md`.

## Окружение

```bash
scripts/bootstrap.sh        # или: make install — .venv (Python 3.11), зависимости, pip install -e ., .env
make up db-init             # PostGIS в Docker (порт из .env, по умолчанию 5433) + схема sql/
make help                   # все таргеты
```

Пакеты не ставить руками поверх общего окружения: нужная зависимость записывается в `LOG.md`, координатор добавляет её в `requirements*.txt`.

## Ветки и коммиты

- Один агент/одна задача — один коммит (D-006). Коммиты в `main` делает координатор после ревью QA.
- Сообщение коммита — на английском, одна строка (максимум две), без многоабзацного тела: `Add matching CLI and IoU tests`.
- Не коммитить `.env`, растры, веса моделей и большие дампы — см. `.gitignore`.

## Границы записи

Каждый пишет только в свои директории по матрице `docs/CONTRACTS.md §1`. Чужие файлы не править; предложения по контрактам — в `LOG.md`, решение фиксирует координатор в `DECISIONS.md`. `PLAN.md`, `DECISIONS.md`, `docs/CONTRACTS.md` — только координатор.

## Стиль и проверки

- Код и комментарии — English; README, docstring-описания тестовых кейсов — русский.
- Перед завершением задачи: `ruff format <свои пути> && ruff check --fix <свои пути>`, затем `pytest tests/<module> -q`. Репо-wide `make lint` только проверяет (`--check`), `make format` — для координатора/QA.
- Хуки: `make pre-commit` (ruff, ruff-format, nbstripout, end-of-file-fixer, check-yaml).
- Ноутбуки в git — без outputs (`nbstripout`), проходят `nbqa ruff notebooks/`.
- Тесты: `tests/<module>/test_*.py`, маркеры `db`, `network`, `slow`, `gpu` (`pyproject.toml`). CI гоняет `pytest -m "not network and not slow and not gpu"` с PostGIS-сервисом.

## CI

`.github/workflows/ci.yml`: `lint` → `test` (PostGIS `postgis/postgis:17-3.6-alpine`, как в `docker-compose.yml`) → `docker` (build + `docker compose config`) → `notebooks`. Локальный аналог: `make ci` (без сборки образа; образ — `make docker-build`).

## Журнал

По завершении задачи — один блок в `LOG.md` (формат `docs/CONTRACTS.md §10`): что сделано, решения, что не получилось, нужные пакеты, команды проверки. Числа в отчётах — только порождённые кодом и лежащие в `reports/`.
