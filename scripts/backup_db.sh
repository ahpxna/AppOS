#!/usr/bin/env bash
set -euo pipefail

# Backs up the jobos-postgres container's database to backups/.
#
# No `sudo` here on purpose: on Docker Desktop (macOS, Windows/WSL2) the
# current user can talk to the Docker daemon directly. On native Linux
# with a root-only Docker socket, add your user to the `docker` group
# (`sudo usermod -aG docker $USER`, then re-login) instead of prefixing
# this script with sudo.

DB_USER="${POSTGRES_USER:-jobos}"
DB_NAME="${JOBOS_DB_NAME:-job_apply_os}"

mkdir -p backups
TS=$(date +"%Y%m%d_%H%M%S")

docker exec jobos-postgres pg_dump -U "$DB_USER" -d "$DB_NAME" \
  --format=custom \
  --file="/tmp/job_apply_os_${TS}.dump"

docker cp \
  "jobos-postgres:/tmp/job_apply_os_${TS}.dump" \
  "backups/job_apply_os_${TS}.dump"

echo "Created backups/job_apply_os_${TS}.dump"
