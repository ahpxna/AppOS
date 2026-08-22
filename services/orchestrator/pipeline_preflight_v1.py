#!/usr/bin/env python3
"""Read-only end-to-end readiness report for the JobOS pipeline.

The historical pipeline failure surfaced only as ``Missing
base_fit_check_support profile_context_pack``.  This preflight makes each
upstream gate explicit before an operator starts an LLM-backed job-fit run.
It never approves evidence, invokes a model, runs a browser task, or writes to
the database.  ``--check-browser`` additionally performs gateway/CDP health
checks, still without opening a page.

Usage:
  python services/orchestrator/pipeline_preflight_v1.py --json
  python services/orchestrator/pipeline_preflight_v1.py --check-browser
  python services/orchestrator/pipeline_preflight_v1.py --require-browser
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.llm_gateway import LLMGatewayError, resolve_config


DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")
DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"

REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSER_WORKER = REPO_ROOT / "services" / "browser-controller" / "browser_queue_worker.py"
BASE_PACK_PURPOSES = (
    "base_fit_check_support",
    "base_resume_generation",
    "base_cover_letter_generation",
    "base_short_answer_generation",
    "base_interview_prep",
    "base_message_reply",
)
REQUIRED_RELATIONS = (
    "applications",
    "profile_assets",
    "profile_capabilities",
    "profile_briefs",
    "profile_context_packs",
    "pipeline_steps",
    "allowed_domains",
)


def item(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    """Create a serialisable preflight item with no secret-bearing fields."""
    return {"name": name, "status": status, "detail": detail, **extra}


def missing_base_packs(present: set[str]) -> list[str]:
    """Return the deterministic profile packs still required by L5/L6 stages."""
    return [purpose for purpose in BASE_PACK_PURPOSES if purpose not in present]


def assess_profile_gate(*, approved_assets: int, approved_capabilities: int,
                        fresh_briefs: int, present_packs: set[str]) -> dict[str, Any]:
    """Explain the exact gate behind a missing profile context pack.

    This function intentionally does not repair state. Evidence approval is a
    human decision; once approved, the deterministic preparation script can
    rebuild briefs and packs without an LLM.
    """
    blockers: list[str] = []
    if approved_assets == 0:
        blockers.append("no approved profile assets")
    if approved_capabilities == 0:
        blockers.append("no approved profile capabilities")
    if fresh_briefs == 0:
        blockers.append("no fresh profile briefs")
    absent = missing_base_packs(present_packs)
    if absent:
        blockers.append("missing base packs: " + ", ".join(absent))
    if blockers:
        return {
            "status": "blocked",
            "detail": "; ".join(blockers),
            "missing_packs": absent,
            "remediation": (
                "Review/approve source-backed profile assets and capabilities, then run "
                "services/profile-ingestion/prepare_profile_for_pipeline_v1.py build --apply."
            ),
        }
    return {
        "status": "pass",
        "detail": "Approved profile evidence, fresh briefs, and all base context packs are present.",
        "missing_packs": [],
    }


def relation_status(cur) -> tuple[list[str], list[dict[str, Any]]]:
    missing: list[str] = []
    checks: list[dict[str, Any]] = []
    for relation in REQUIRED_RELATIONS:
        cur.execute("SELECT to_regclass(%s);", (f"public.{relation}",))
        exists = cur.fetchone()[0] is not None
        checks.append(item(f"relation:{relation}", "pass" if exists else "blocked",
                           "present" if exists else "missing; apply migrations"))
        if not exists:
            missing.append(relation)
    return missing, checks


def database_report() -> list[dict[str, Any]]:
    """Collect schema and state counts with read-only SQL queries."""
    checks: list[dict[str, Any]] = []
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                missing, relation_checks = relation_status(cur)
                checks.extend(relation_checks)
                if missing:
                    return checks

                cur.execute("SELECT count(*) FROM profile_assets WHERE status = 'approved';")
                approved_assets = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM profile_capabilities WHERE status = 'approved';")
                approved_capabilities = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM profile_briefs WHERE is_stale = false;")
                fresh_briefs = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT purpose FROM profile_context_packs
                    WHERE application_id IS NULL AND message_thread_id IS NULL
                      AND purpose = ANY(%s);
                    """,
                    (list(BASE_PACK_PURPOSES),),
                )
                present_packs = {row[0] for row in cur.fetchall()}
                checks.append(item(
                    "profile_context_gate",
                    **assess_profile_gate(
                        approved_assets=approved_assets,
                        approved_capabilities=approved_capabilities,
                        fresh_briefs=fresh_briefs,
                        present_packs=present_packs,
                    ),
                    approved_assets=approved_assets,
                    approved_capabilities=approved_capabilities,
                    fresh_briefs=fresh_briefs,
                    present_base_packs=sorted(present_packs),
                ))

                cur.execute("SELECT count(*) FROM applications WHERE status = 'active';")
                active_jobs = int(cur.fetchone()[0])
                checks.append(item(
                    "captured_jobs", "pass" if active_jobs else "warning",
                    f"{active_jobs} active captured job(s)." if active_jobs else
                    "No active jobs yet; use ATS discovery or user-reviewed LinkedIn intake.",
                    active_jobs=active_jobs,
                ))
                cur.execute("SELECT count(*) FROM allowed_domains WHERE enabled = true;")
                allowed_domains = int(cur.fetchone()[0])
                checks.append(item(
                    "browser_allowlist", "pass" if allowed_domains else "warning",
                    f"{allowed_domains} enabled domain(s)." if allowed_domains else
                    "Browser tasks will refuse every URL until a domain is deliberately added.",
                    enabled_domains=allowed_domains,
                ))
    except psycopg.Error as exc:
        checks.append(item("database", "blocked", f"Database unavailable: {str(exc).splitlines()[0][:300]}"))
    return checks


def llm_config_report() -> dict[str, Any]:
    """Validate provider selection only; no token or network request is made."""
    try:
        config = resolve_config(role="job_fit")
    except LLMGatewayError as exc:
        return item("llm_transport", "blocked", str(exc))
    return item(
        "llm_transport", "pass",
        f"role=job_fit uses backend={config.backend}, model={config.model}, style={config.api_style}.",
        backend=config.backend, model=config.model, api_style=config.api_style,
    )


def browser_report() -> dict[str, Any]:
    """Run only the worker's no-task health probe and retain bounded output."""
    try:
        proc = subprocess.run(
            [sys.executable, str(BROWSER_WORKER), "--health"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=50,
        )
    except subprocess.TimeoutExpired:
        return item("browser_runtime", "blocked", "Browser health check timed out.")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()[-1200:]
    return item(
        "browser_runtime", "pass" if proc.returncode == 0 else "blocked",
        output or f"health command exited {proc.returncode}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only JobOS pipeline preflight.")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    parser.add_argument("--check-browser", action="store_true", help="Also check the OpenClaw gateway and Chrome CDP listener.")
    parser.add_argument("--require-browser", action="store_true", help="Treat unavailable browser runtime as a pipeline blocker.")
    args = parser.parse_args()
    if args.require_browser:
        args.check_browser = True

    checks = database_report()
    checks.append(llm_config_report())
    if args.check_browser:
        browser = browser_report()
        if browser["status"] == "blocked" and not args.require_browser:
            browser["status"] = "warning"
        checks.append(browser)

    blocked = [check for check in checks if check["status"] == "blocked"]
    report = {
        "report_type": "jobos_pipeline_preflight_v1",
        "writes": False,
        "checks": checks,
        "ready": not blocked,
        "next": "Resolve each blocked check before an end-to-end L5/L6 run. Warnings do not block a core profile/JD run.",
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for check in checks:
            print(f"[{check['status'].upper():7}] {check['name']}: {check['detail']}")
        print("\nREADY" if report["ready"] else "\nNOT READY")
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
