#!/usr/bin/env bash
set -euo pipefail

# Applies every file in db/migrations/ in plain filename-sort order, against
# the job_apply_os database. There is no migration-tracking table in this
# project (see db/migrations/README.md) -- this script is meant to be run
# once against a brand-new database, right after `docker compose up -d`
# (or a native Postgres) has created an empty job_apply_os database.
#
# Safe to re-run: every migration file uses IF NOT EXISTS / CREATE OR
# REPLACE / ON CONFLICT DO NOTHING patterns, so re-applying against a
# database that already has them is a no-op, not an error -- but this is
# NOT guaranteed for hand-edited or future migrations, so treat re-running
# on a database with real data as something to check first, not assume.

cd "$(dirname "$0")/.."

DB_HOST="${JOBOS_DB_HOST:-127.0.0.1}"
DB_PORT="${JOBOS_DB_PORT:-${POSTGRES_HOST_PORT:-5433}}"
DB_NAME="${JOBOS_DB_NAME:-job_apply_os}"
DB_USER="${JOBOS_DB_USER:-${POSTGRES_USER:-jobos}}"
export PGPASSWORD="${JOBOS_DB_PASSWORD:-${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD or JOBOS_DB_PASSWORD in your environment or .env}}"

if ! command -v psql >/dev/null 2>&1; then
  echo "psql not found. Install the postgresql-client package (e.g. 'sudo apt install postgresql-client' on WSL/Debian/Ubuntu), or run this against the postgres container instead:" >&2
  echo "  docker exec -i jobos-postgres psql -U $DB_USER -d $DB_NAME < db/migrations/<file>.sql" >&2
  exit 1
fi

count=0
for f in db/migrations/*.sql; do
  echo "==> applying $f"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$f"
  count=$((count + 1))
done

echo ""
echo "Applied $count migration files to $DB_NAME @ $DB_HOST:$DB_PORT"
