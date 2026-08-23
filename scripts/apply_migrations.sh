#!/usr/bin/env bash
set -euo pipefail

# Compatibility wrapper for the checksum-tracked Python migration runner.
# The runner loads .env, does not need psql, and never re-runs an applied file.

cd "$(dirname "$0")/.."

exec python3 scripts/apply_migrations.py "$@"
