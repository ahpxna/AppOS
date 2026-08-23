# Ubuntu 24.04 bootstrap

For a fresh Ubuntu machine, run only this after cloning the repository:

```bash
git clone <your-private-repo-url> job-apply-os
cd job-apply-os
bash scripts/bootstrap_ubuntu_24.sh
```

The bootstrap installs the Python virtual-environment and OCR packages, makes
an untracked `.env` with fresh local passwords, creates `.venv`, installs every
Python requirement (including `psycopg[binary]`), starts PostgreSQL, waits for
it, then runs checksum-tracked migrations. Add `--with-n8n` only when the n8n
UI is wanted.

Every JobOS executable explicitly loads the untracked `.env` through the
shared configuration module. Do **not** run `source .env`; activating `.venv`
is the only shell step required.

It deliberately does **not** pull an Ollama model, start OpenClaw, start a
market-intelligence worker, or use a paid token API. Those are separate,
opt-in stages.

## Prerequisites

Install Git and Docker Engine/Desktop with the Compose v2 plugin first. The
script checks for `docker compose` and fails before modifying the database if
it is unavailable. On a non-Docker PostgreSQL deployment, skip the bootstrap
database section and invoke `.venv/bin/python scripts/apply_migrations.py`
after setting the `JOBOS_DB_*` variables in `.env`.

## Daily start

```bash
cd job-apply-os
source .venv/bin/activate
docker compose up -d postgres
python scripts/apply_migrations.py
pytest -q
```

The migration command is idempotent because it records each filename and its
SHA-256 checksum in `schema_migrations`. It does not replay seed/update SQL.
If a migration file already applied has been edited, it fails rather than
guessing. Add a new migration instead.

## Existing database created before the ledger

Do not run the full history again. After checking that the old database has
successfully applied migrations through 050, adopt that verified state once:

```bash
source .venv/bin/activate
python scripts/apply_migrations.py --adopt-existing --through 050
```

The command records checksums for those historical files and then applies 051
and future migrations normally. This is intentionally explicit because a
partially migrated database must not be silently marked healthy.

## Optional LLM and browser stages

Core JD intake, ranking, evidence storage, resume rendering, and tests do not
need a model. Configure either Ollama or an OpenAI-compatible API later in
`.env`; see `.env.example` and [ubuntu_gpu.md](ubuntu_gpu.md). OpenClaw is only
a browser-execution adapter and remains opt-in; see [openclaw.md](openclaw.md).

Before any browser pilot, run the disposable database lifecycle gate documented
in [autofill_architecture.md](autofill_architecture.md). Start only the local
fixtures next; never use a real ATS as the first execution target.
