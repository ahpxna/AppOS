#!/usr/bin/env python3
"""Apply each JobOS migration exactly once, with checksum protection.

Fresh installs simply run this command. Existing databases created before this
ledger are deliberately not guessed at: adopt their verified history once with
``--adopt-existing --through 050`` before applying later migrations.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"
MIGRATION_RE = re.compile(r"^(\d+)")


def load_dotenv(path: Path) -> None:
    """Read the simple KEY=VALUE form used by JobOS without another package."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def connection_string() -> str:
    load_dotenv(ROOT / ".env")
    host = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
    port = os.getenv("JOBOS_DB_PORT", os.getenv("POSTGRES_HOST_PORT", "5433"))
    name = os.getenv("JOBOS_DB_NAME", "job_apply_os")
    user = os.getenv("JOBOS_DB_USER", os.getenv("POSTGRES_USER", "jobos"))
    password = os.getenv("JOBOS_DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", ""))
    if not password or password.startswith("CHANGE_ME"):
        raise RuntimeError("Set a real POSTGRES_PASSWORD/JOBOS_DB_PASSWORD in .env first.")
    return f"host={host} port={port} dbname={name} user={user} password={password}"


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"), key=lambda path: path.name)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migration_number(path: Path) -> int:
    match = MIGRATION_RE.match(path.name)
    if not match:
        raise RuntimeError(f"Migration lacks numeric prefix: {path.name}")
    return int(match.group(1))


def strip_outer_transaction(sql: str) -> str:
    """Let the runner atomically add the migration ledger row with the SQL."""
    # Existing files usually open the transaction after a header comment, so
    # anchor to a standalone SQL line instead of assuming BEGIN is byte zero.
    # PL/pgSQL function bodies use ``BEGIN`` without this standalone semicolon.
    sql = re.sub(r"(?im)^\s*BEGIN;\s*$", "", sql, count=1)
    return re.sub(r"(?im)^\s*COMMIT;\s*$", "", sql, count=1)


def ensure_ledger(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          migration_id text PRIMARY KEY,
          checksum_sha256 text NOT NULL,
          applied_at timestamptz NOT NULL DEFAULT now(),
          runner_version text NOT NULL
        );
        """
    )


def is_legacy_database(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT to_regclass('public.applications') IS NOT NULL;").fetchone()[0]


def adopt_existing(conn: psycopg.Connection, through: int) -> int:
    adopted = 0
    for path in migration_files():
        if migration_number(path) > through:
            continue
        conn.execute(
            """
            INSERT INTO schema_migrations (migration_id, checksum_sha256, runner_version)
            VALUES (%s, %s, 'ledger-v1-adopted')
            ON CONFLICT (migration_id) DO NOTHING;
            """,
            (path.name, checksum(path)),
        )
        adopted += 1
    return adopted


def apply(args: argparse.Namespace) -> int:
    files = migration_files()
    if not files:
        raise RuntimeError("No migration files found.")
    with psycopg.connect(connection_string(), autocommit=True) as conn:
        ensure_ledger(conn)
        existing_rows = conn.execute("SELECT migration_id, checksum_sha256 FROM schema_migrations;").fetchall()
        applied = {str(migration_id): str(file_checksum) for migration_id, file_checksum in existing_rows}
        if not applied and is_legacy_database(conn):
            if not args.adopt_existing:
                raise RuntimeError(
                    "Existing database has no migration ledger. Verify its state, then run: "
                    "python scripts/apply_migrations.py --adopt-existing --through 050"
                )
            adopted = adopt_existing(conn, args.through)
            print(f"Adopted {adopted} already-applied migration file(s) through {args.through}.")
            applied = {str(mid): str(digest) for mid, digest in conn.execute(
                "SELECT migration_id, checksum_sha256 FROM schema_migrations;"
            ).fetchall()}

        for path in files:
            current = checksum(path)
            recorded = applied.get(path.name)
            if recorded:
                if recorded != current:
                    raise RuntimeError(
                        f"Checksum mismatch for already-applied {path.name}. "
                        "Do not edit shipped migrations; create a new migration instead."
                    )
                continue
            if args.dry_run:
                print(f"Would apply {path.name}")
                continue
            print(f"Applying {path.name}")
            sql = strip_outer_transaction(path.read_text(encoding="utf-8"))
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (migration_id, checksum_sha256, runner_version)
                    VALUES (%s, %s, 'ledger-v1');
                    """,
                    (path.name, current),
                )
    print("Migrations are current.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply JobOS migrations once with checksum protection.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--adopt-existing", action="store_true", help="adopt a verified pre-ledger database")
    parser.add_argument("--through", type=int, default=50, help="highest migration number to adopt (default: 50)")
    args = parser.parse_args()
    if args.adopt_existing and args.through < 1:
        parser.error("--through must be positive")
    try:
        return apply(args)
    except (RuntimeError, psycopg.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
