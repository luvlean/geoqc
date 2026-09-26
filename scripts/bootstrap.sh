#!/usr/bin/env bash
# Bootstrap a development environment from scratch:
#   python venv -> pip upgrade -> requirements-dev.txt -> pip install -e . -> .env from .env.example
# Idempotent: re-running upgrades dependencies in place. Never touches the database.
#
# Env knobs: PYTHON (interpreter, default python3.11 or python3), VENV (default .venv),
#            GEOQC_TORCH_CPU=1 installs CPU-only torch first (smaller, no CUDA),
#            GEOQC_PRECOMMIT=0 skips `pre-commit install`.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV=${VENV:-.venv}
PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
  if command -v python3.11 >/dev/null 2>&1; then PYTHON=python3.11; else PYTHON=python3; fi
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 13) else 1)'; then
  echo "error: $PYTHON is $("$PYTHON" --version 2>&1); geoqc needs Python >=3.11,<3.13 (set PYTHON=...)" >&2
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating virtualenv $VENV with $PYTHON"
  "$PYTHON" -m venv "$VENV"
else
  echo "==> reusing virtualenv $VENV"
fi

PIP="$VENV/bin/python -m pip"
echo "==> upgrading pip"
$PIP install --quiet --upgrade pip

if [ "${GEOQC_TORCH_CPU:-0}" = "1" ]; then
  echo "==> installing CPU-only torch/torchvision"
  $PIP install --index-url https://download.pytorch.org/whl/cpu torch torchvision
fi

echo "==> installing requirements-dev.txt (includes requirements.txt)"
$PIP install -r requirements-dev.txt

echo "==> installing geoqc in editable mode"
$PIP install -e .

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> created .env from .env.example (edit POSTGRES_PORT / ARCGIS_API_KEY if needed)"
else
  echo "==> .env already exists, left untouched"
fi

if [ "${GEOQC_PRECOMMIT:-1}" = "1" ] && [ -d .git ] && [ -x "$VENV/bin/pre-commit" ]; then
  echo "==> installing pre-commit hooks"
  "$VENV/bin/pre-commit" install >/dev/null
fi

echo
echo "done. Next steps:"
echo "  make up db-init     # PostGIS on port \${POSTGRES_PORT} + schema"
echo "  make test           # fast test suite"
echo "  $VENV/bin/geoqc --help"
