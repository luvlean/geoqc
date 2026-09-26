# geoqc — top-level automation.
# Compatible with GNU make 3.81 (macOS): no .ONESHELL, no make-4.x functions.
# Every pipeline target calls a CLI command from docs/CONTRACTS.md §6, even when the
# owning module is not delivered yet (the target then fails with "No such command").
#
#   make help            list targets
#   make up db-init      start PostGIS and apply sql/
#   make all             up db-init data detect match eval
#   make demo            API + web map on http://localhost:8000

PY      ?= .venv/bin/python
GEOQC   ?= .venv/bin/geoqc
PYTEST  ?= .venv/bin/pytest
RUFF    ?= .venv/bin/ruff
COMPOSE ?= docker compose

# Pipeline knobs — override on the command line, e.g. `make detect SPLIT=val`.
SPLIT          ?= all
PRE_SOURCE     ?= secondary
VERIFICATION   ?= reports/verification.csv
PYTEST_MARKERS ?= not network and not slow and not gpu
HOST           ?= 0.0.0.0
PORT           ?= 8000
# model_version comes from config/cv.yaml (CV Agent). Override: `make match MODEL_VERSION=<v>`.
# Recursive (?=) on purpose: the shell call runs only when a target references it.
MODEL_VERSION  ?= $(shell $(PY) -c "from geoqc.config import load_config; print(load_config().get('cv', {}).get('model_version', '') or '')" 2>/dev/null)

.DEFAULT_GOAL := help
.PHONY: help install up down db-init db-init-test data labels-prep labels-validate detect match eval \
        demo demo-docker all test test-all test-cov lint format bundle ci compose-config docker-build \
        notebooks-check pre-commit clean

help: ## Show this help
	@echo "geoqc — make targets (GNU make 3.81+). Variables: PY=$(PY) GEOQC=$(GEOQC)"
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

# ---------------------------------------------------------------- environment

install: ## Create .venv, install requirements-dev.txt + package (editable), copy .env.example -> .env
	scripts/bootstrap.sh

up: ## Start PostGIS via docker compose and wait until healthy
	$(COMPOSE) up -d --wait db

down: ## Stop containers (the pgdata volume is kept; `docker compose down -v` drops it)
	$(COMPOSE) down

db-init: ## Apply sql/*.sql to DATABASE_URL (geoqc matching init-db; fallback: scripts/db_init.py)
	@if $(GEOQC) matching --help >/dev/null 2>&1; then \
		echo "$(GEOQC) matching init-db"; \
		$(GEOQC) matching init-db; \
	else \
		echo "geoqc matching is not available yet -> fallback: $(PY) scripts/db_init.py"; \
		$(PY) scripts/db_init.py; \
	fi

db-init-test: ## Apply sql/*.sql to TEST_DATABASE_URL (tests/conftest.py does this too)
	$(PY) scripts/db_init.py --test

# ---------------------------------------------------------------- pipeline (docs/CONTRACTS.md §6)

data: ## Imagery + OSM: download, preprocess, tile, load buildings_ref (geoqc data all)
	$(GEOQC) data all

labels-prep: ## Select tiles, pre-annotate val/test drafts, generate the QGIS project
	$(GEOQC) annotation select-tiles
	$(GEOQC) annotation pre-annotate --split val --source $(PRE_SOURCE)
	$(GEOQC) annotation pre-annotate --split test --source $(PRE_SOURCE)
	$(GEOQC) annotation qgis-project --split val
	$(GEOQC) annotation qgis-project --split test

labels-validate: ## Export QGIS work.gpkg (finalize) and validate labels -> reports/annotation.json
	$(GEOQC) annotation finalize --split val
	$(GEOQC) annotation finalize --split test
	$(GEOQC) annotation validate --split val
	$(GEOQC) annotation validate --split test

detect: ## Fetch weights, run detection on tiles -> detections.geojson + buildings_det
	$(GEOQC) cv fetch-weights
	$(GEOQC) cv predict --split $(SPLIT) --to-db

match: ## Match det vs ref, classify discrepancies, export queue, print stats
	@mv="$(MODEL_VERSION)"; \
	if [ -z "$$mv" ]; then \
		echo "MODEL_VERSION is empty: set model_version in config/cv.yaml or run 'make match MODEL_VERSION=<v>'" >&2; \
		exit 1; \
	fi; \
	set -x; \
	$(GEOQC) matching run --model-version "$$mv" && \
	$(GEOQC) matching export && \
	$(GEOQC) matching stats

eval: ## Detection metrics (val/test), verification sample, error analysis, gates -> reports/metrics.json
	$(GEOQC) eval detection --split val
	$(GEOQC) eval detection --split test
	$(GEOQC) eval verification-sample
	if [ -f "$(VERIFICATION)" ]; then \
		$(GEOQC) eval discrepancies --verification "$(VERIFICATION)"; \
	else \
		echo "skip: $(VERIFICATION) not found (fill reports/verification_sample.csv by hand, save as $(VERIFICATION))"; \
	fi
	$(GEOQC) eval error-analysis
	$(GEOQC) eval compare-secondary
	$(GEOQC) eval gates

demo: up db-init ## PostGIS + API + web map from the local venv (http://localhost:8000)
	$(GEOQC) api serve --host $(HOST) --port $(PORT)

demo-docker: ## Same via docker compose (builds the app image; APP_PORT in .env, default 8000)
	$(COMPOSE) up -d --build --wait db app
	@echo "geoqc app is up: http://localhost:8000 (override with APP_PORT in .env)"

all: up db-init data detect match eval ## Full pipeline: up db-init data detect match eval

bundle: ## Zip tiles + val labels + config for Colab fine-tuning -> data/interim/bundle.zip
	$(GEOQC) cv bundle

# ---------------------------------------------------------------- quality

test: ## Fast tests (no network/slow/gpu); db tests skip themselves when PostGIS is unreachable
	$(PYTEST) -q -m "$(PYTEST_MARKERS)"

test-all: ## Every test, including network (GEOQC_NETWORK=1), slow and gpu
	GEOQC_NETWORK=1 $(PYTEST) -q

test-cov: ## Fast tests with coverage (coverage.xml), same command as CI
	$(PYTEST) -q -m "$(PYTEST_MARKERS)" --cov=geoqc --cov-report=xml --cov-report=term

lint: ## ruff format --check + ruff check on the whole repo (read-only)
	$(RUFF) format --check .
	$(RUFF) check .

format: ## ruff format + ruff check --fix on the whole repo (coordinator/QA only; module agents use their own paths)
	$(RUFF) format .
	$(RUFF) check --fix .

notebooks-check: ## nbqa ruff + nbstripout --verify on notebooks/ (no-op while there are none)
	scripts/ci/check_notebooks.sh

compose-config: ## Validate docker-compose.yml
	$(COMPOSE) config -q

docker-build: ## Build the app image locally (first build is slow: CPU torch wheels)
	docker build -t geoqc-app:local .

ci: lint test-cov notebooks-check compose-config ## Local equivalent of the GitHub Actions jobs (docker build excluded)

pre-commit: ## Install git hooks from .pre-commit-config.yaml
	.venv/bin/pre-commit install

clean: ## Remove caches and build artifacts (data/, models/, reports/ are kept)
	find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov build dist
