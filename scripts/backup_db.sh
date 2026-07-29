#!/usr/bin/env bash
set -euo pipefail

mkdir -p backups
TS=$(date +"%Y%m%d_%H%M%S")

sudo docker exec jobos-postgres pg_dump -U jobos -d job_apply_os \
  --format=custom \
  --file="/tmp/job_apply_os_${TS}.dump"

sudo docker cp \
  "jobos-postgres:/tmp/job_apply_os_${TS}.dump" \
  "backups/job_apply_os_${TS}.dump"

echo "Created backups/job_apply_os_${TS}.dump"
