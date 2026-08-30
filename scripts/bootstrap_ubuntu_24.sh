#!/usr/bin/env bash
# One-command local bootstrap for a fresh Ubuntu 24.04 JobOS workstation.
# It intentionally does NOT install/pull an LLM, start OpenClaw, or create
# external accounts. Core intake, DB, tests, and document code work without it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

need_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command '$1' was not found. $2" >&2
    exit 1
  }
}

if [[ "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage: bash scripts/bootstrap_ubuntu_24.sh [--with-n8n]

Creates .venv, installs Python/system dependencies, creates a local .env with
fresh development secrets if absent, starts PostgreSQL, and applies migrations.
It does not start Ollama, OpenClaw, or any token-charging worker.
EOF
  exit 0
fi
if [[ $# -gt 1 || ($# -eq 1 && "${1:-}" != "--with-n8n") ]]; then
  echo "ERROR: unknown option. Usage: bash scripts/bootstrap_ubuntu_24.sh [--with-n8n]" >&2
  exit 2
fi

need_command python3 "Install Python 3.11+ and retry."
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "ERROR: JobOS requires Python 3.11+ (it uses enum.StrEnum)." >&2
  exit 1
}
need_command docker "Install Docker Engine/Desktop plus the Docker Compose plugin, then retry."
docker compose version >/dev/null 2>&1 || {
  echo "ERROR: Docker Compose v2 plugin is required (the 'docker compose' command)." >&2
  exit 1
}

if command -v apt-get >/dev/null 2>&1; then
  echo "Installing Ubuntu packages (Python venv/pip and optional OCR binaries)..."
  apt_prefix=()
  if [[ ${EUID} -ne 0 ]]; then
    need_command sudo "Run as root or install sudo, then retry."
    apt_prefix=(sudo)
  fi
  "${apt_prefix[@]}" apt-get update
  "${apt_prefix[@]}" apt-get install -y \
    python3-venv python3-pip python3-tk poppler-utils tesseract-ocr libreoffice-writer
fi

if [[ ! -f .env ]]; then
  db_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  n8n_key="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  gateway_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cp .env.example .env
  sed -i \
    -e "s|POSTGRES_PASSWORD=CHANGE_ME_local_dev_password|POSTGRES_PASSWORD=${db_password}|" \
    -e "s|JOBOS_DB_PASSWORD=CHANGE_ME_local_dev_password|JOBOS_DB_PASSWORD=${db_password}|" \
    -e "s|N8N_ENCRYPTION_KEY=CHANGE_ME_random_32_char_string|N8N_ENCRYPTION_KEY=${n8n_key}|" \
    -e "s|OPENCLAW_GATEWAY_TOKEN=CHANGE_ME_random_token|OPENCLAW_GATEWAY_TOKEN=${gateway_token}|" \
    .env
  chmod 600 .env
  echo "Created .env with local development secrets (kept untracked)."
else
  echo "Using existing .env; it was not changed."
fi

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install \
  -r requirements.txt \
  -c constraints-v1.txt

compose_services=(postgres)
if [[ "${1:-}" == "--with-n8n" ]]; then
  compose_services+=(n8n)
fi
docker compose up -d "${compose_services[@]}"

echo "Waiting for PostgreSQL to accept connections..."
for _ in $(seq 1 30); do
  if docker exec jobos-postgres pg_isready -U jobos -d job_apply_os >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec jobos-postgres pg_isready -U jobos -d job_apply_os >/dev/null

.venv/bin/python scripts/migration_lint.py
.venv/bin/python scripts/apply_migrations.py
.venv/bin/python scripts/jobos.py doctor --profile core --strict
echo
echo "Bootstrap complete. Next: source .venv/bin/activate"
echo "Optional LLM/OpenClaw setup is separate; see docs/ubuntu_bootstrap.md."
