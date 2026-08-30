#!/usr/bin/env bash
set -euo pipefail

PYTHON="${JOBOS_PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: supported JobOS virtualenv is missing. Run bash scripts/bootstrap_ubuntu_24.sh first." >&2
  exit 1
fi

"$PYTHON" scripts/migration_lint.py
"$PYTHON" scripts/apply_migrations.py
"$PYTHON" scripts/jobos.py doctor --profile "${JOBOS_VERIFY_PROFILE:-core}" --strict
"$PYTHON" -m pytest -q
