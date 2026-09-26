#!/usr/bin/env bash
# Notebook checks shared by `make notebooks-check` and the CI `notebooks` job:
#   1) nbqa ruff  — lint code cells with the repo ruff config
#   2) nbstripout --verify — notebooks must be committed without outputs
# No-op (exit 0) while the notebooks directory has no .ipynb files.
# Env: NOTEBOOKS_DIR (default notebooks), NBQA / NBSTRIPOUT (binaries; default .venv/bin/* or PATH)
set -euo pipefail
cd "$(dirname "$0")/../.."

DIR=${NOTEBOOKS_DIR:-notebooks}
if [ -z "${NBQA:-}" ]; then
  if [ -x .venv/bin/nbqa ]; then NBQA=.venv/bin/nbqa; else NBQA=nbqa; fi
fi
if [ -z "${NBSTRIPOUT:-}" ]; then
  if [ -x .venv/bin/nbstripout ]; then NBSTRIPOUT=.venv/bin/nbstripout; else NBSTRIPOUT=nbstripout; fi
fi

FILES=$(find "$DIR" -name '*.ipynb' -not -path '*/.ipynb_checkpoints/*' 2>/dev/null | sort || true)
if [ -z "$FILES" ]; then
  echo "notebooks: no .ipynb files under $DIR/ — nothing to check"
  exit 0
fi

echo "notebooks under $DIR/:"
printf '  %s\n' $FILES

echo "==> nbqa ruff $DIR/"
"$NBQA" ruff "$DIR/"

echo "==> nbstripout --verify"
# shellcheck disable=SC2086
"$NBSTRIPOUT" --verify $FILES
echo "notebooks: OK (linted, no stored outputs)"
