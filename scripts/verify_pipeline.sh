#!/usr/bin/env bash
set -euo pipefail

python scripts/migration_lint.py
python scripts/apply_migrations.py
python scripts/jobos.py doctor --strict
python -m pytest -q
