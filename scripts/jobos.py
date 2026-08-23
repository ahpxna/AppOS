#!/usr/bin/env python3
"""Small operator entrypoint for safe JobOS readiness checks."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.common.config import database_dsn, load_repo_env
from services.common.openclaw_runtime import resolve_openclaw_binary


def mark(results: list[tuple[str, bool, str]], name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def doctor(*, check_browser: bool) -> int:
    """Report readiness without invoking a model, mutating data, or opening a tab."""
    load_repo_env()
    results: list[tuple[str, bool, str]] = []
    mark(results, "Python 3.11+", sys.version_info >= (3, 11), sys.version.split()[0])
    env_path = ROOT / ".env"
    mark(results, "Environment", env_path.is_file(), str(env_path) if env_path.is_file() else "run bootstrap")
    template = ROOT / "data" / "resume-template" / "VU PHAN AN NGUYEN-official_For_all.docx"
    mark(results, "Resume template", template.is_file(), str(template))
    upload_root = Path(os.getenv("JOBOS_OPENCLAW_UPLOADS_DIR", "/tmp/openclaw/uploads"))
    upload_parent = upload_root.parent
    while not upload_parent.exists() and upload_parent != upload_parent.parent:
        upload_parent = upload_parent.parent
    mark(results, "Managed upload root", os.access(upload_parent, os.W_OK), str(upload_root))

    try:
        binary = resolve_openclaw_binary(required=True)
        mark(results, "OpenClaw runtime", Path(binary).is_file() or bool(os.path.basename(binary)), binary)
    except RuntimeError as exc:
        mark(results, "OpenClaw runtime", False, str(exc))

    try:
        import psycopg
        with psycopg.connect(database_dsn(), connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id = '055_autofill_exact_input_and_page_identity.sql')")
            mark(results, "Migrations through 055", bool(cur.fetchone()[0]))
            cur.execute("SELECT to_regclass('public.autofill_action_journal') IS NOT NULL")
            mark(results, "Autofill action journal", bool(cur.fetchone()[0]))
            cur.execute("SELECT count(*) FROM browser_tasks WHERE execution_state = 'needs_reconciliation'")
            unresolved = int(cur.fetchone()[0])
            mark(results, "No unresolved autofill task", unresolved == 0, f"{unresolved} unresolved")
            cur.execute("SELECT count(*) FROM immigration_profiles WHERE profile_key = 'primary' AND user_confirmed_at IS NOT NULL")
            mark(results, "Immigration profile confirmed", int(cur.fetchone()[0]) == 1)
    except Exception as exc:
        mark(results, "PostgreSQL / migration state", False, str(exc)[:180])

    if check_browser:
        # Health only: it validates gateway/CDP availability but does not list
        # tabs, read a page, run an agent, or issue a browser write.
        import subprocess
        result = subprocess.run([sys.executable, str(ROOT / "services" / "browser-controller" / "browser_queue_worker.py"), "--health"],
                                capture_output=True, text=True, timeout=30)
        mark(results, "Gateway and CDP health", result.returncode == 0,
             (result.stdout or result.stderr).strip().replace("\n", " ")[:180])

    print("JOBOS DOCTOR\n")
    for name, ok, detail in results:
        print(f"{'✓' if ok else '⚠'} {name}" + (f" — {detail}" if detail else ""))
    core = all(ok for name, ok, _ in results if name in {"Python 3.11+", "Environment", "Migrations through 055"})
    autofill = core and all(ok for name, ok, _ in results if name in {
        "Autofill action journal", "No unresolved autofill task", "Immigration profile confirmed",
        "OpenClaw runtime", "Managed upload root",
    })
    print(f"\nCORE READY: {'YES' if core else 'NO'}")
    print(f"AUTOFILL READY: {'YES' if autofill else 'NO'}")
    print("SUBMIT: HUMAN ONLY")
    return 0 if core else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS operator commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_parser = commands.add_parser("doctor", help="Read-only readiness and safety checks.")
    doctor_parser.add_argument("--check-browser", action="store_true", help="Also probe gateway and CDP health; never opens a page.")
    args = parser.parse_args()
    return doctor(check_browser=args.check_browser)


if __name__ == "__main__":
    raise SystemExit(main())
