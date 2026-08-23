#!/usr/bin/env python3
"""Small operator entrypoint for safe JobOS readiness and browser preparation."""
from __future__ import annotations

import argparse
import os
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.common.config import database_dsn, load_repo_env
from services.common.autofill_identity import canonical_page_url, page_fingerprint
from services.common.autofill_field_registry import AUTOFILL_FIELD_REGISTRY
from services.common.openclaw_runtime import resolve_openclaw_binary
from services.autofill.autofill_executor_v1 import OpenClawTransport
from services.autofill.autofill_agent_v1 import parse_snapshot
from services.autofill.autofill_planner_v1 import plan_autofill
from services.autofill.form_inspector_v1 import inspect_nodes, inspect_question_groups


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


def _origin(url: str) -> str:
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("The pinned browser tab has no HTTP(S) application URL.")
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def _load_prepare_profile(cur) -> tuple[dict[str, object], dict[str, object]]:
    """Read approved static/sensitive values without exposing them to an agent."""
    cur.execute("SELECT field_name, field_value FROM v_autofill_ready_values;")
    ready = {str(name): str(value) for name, value in cur.fetchall()
             if str(value).strip() and str(value) != "FILL_ME"}
    profile: dict[str, object] = {
        "personal": {}, "address": {}, "education": {}, "employment": {},
        "preferences": {}, "documents": {}, "_approval_ready_values": ready,
    }
    for source, target in AUTOFILL_FIELD_REGISTRY.items():
        if source in ready:
            profile[target[0]][target[1]] = ready[source]  # type: ignore[index]
    personal = profile["personal"]  # type: ignore[assignment]
    if personal.get("first_name") and personal.get("last_name"):
        personal["full_name"] = f"{personal['first_name']} {personal['last_name']}"

    cur.execute(
        """SELECT current_work_authorization, requires_sponsorship_to_start,
                  requires_future_sponsorship, us_citizen, us_person,
                  permanent_work_authorization, user_confirmed_at, confirmation_version
             FROM immigration_profiles WHERE profile_key = 'primary';"""
    )
    row = cur.fetchone()
    answers: dict[str, object] = {}
    if row and row[6] and int(row[7] or 0) >= 1:
        for key, value in zip(("CURRENT_AUTHORIZATION", "SPONSORSHIP_TO_START",
                               "SPONSORSHIP_NOW_OR_FUTURE", "US_CITIZENSHIP",
                               "US_PERSON", "PERMANENT_WORK_AUTHORIZATION"), row[:6]):
            if str(value).casefold() in {"yes", "no"}:
                answers[key] = {"value": str(value).title(), "confirmed_at": str(row[6]),
                                "confirmation_version": int(row[7])}
    return profile, answers


def autofill_prepare(application_id: str, *, create: bool, yes: bool) -> int:
    """Pin the current tab and create an exact approval without user-supplied IDs.

    This is the only product-facing bridge into the deterministic autofill
    approval flow. It reads the tab, profile, and approved resume locally;
    no OpenClaw agent or LLM receives applicant data in this path.
    """
    load_repo_env()
    import psycopg
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT company, job_title, coalesce(ats_type, 'unknown') FROM applications WHERE id = %s;", (application_id,))
        application = cur.fetchone()
        if not application:
            raise RuntimeError("Application was not found.")
        cur.execute("SELECT autofill_mode FROM ats_capabilities WHERE ats_type = %s;", (application[2],))
        capability = cur.fetchone()
        if not capability or capability[0] == "review_only":
            raise RuntimeError(f"ATS '{application[2]}' is review-only or unregistered for deterministic autofill.")
        cur.execute(
            """SELECT gd.id::text, gda.id::text, gda.filename
                 FROM generated_documents gd
                 LEFT JOIN generated_document_artifacts gda
                   ON gda.generated_document_id = gd.id AND gda.application_id = gd.application_id
                 WHERE gd.application_id = %s AND gd.doc_type = 'resume'
                   AND gd.qa_status = 'pass' AND gd.approved = true
                 ORDER BY gda.created_at DESC NULLS LAST, gd.created_at DESC
                 LIMIT 1;""",
            (application_id,),
        )
        document = cur.fetchone()
        if not document:
            raise RuntimeError("No verified, user-approved resume exists for this application.")
        profile, sensitive = _load_prepare_profile(cur)
        transport = OpenClawTransport(
            binary=resolve_openclaw_binary(required=True),
            profile=os.getenv("JOBOS_BROWSER_PROFILE", "remote"),
            timeout=90,
        )
        target = transport.resolve_target()
        actual_url = transport.current_url(target.target_id)
        from urllib.parse import urlsplit
        host = (urlsplit(actual_url).hostname or "").casefold()
        cur.execute("SELECT domain FROM allowed_domains WHERE enabled = true;")
        allowed = [str(row[0]).casefold() for row in cur.fetchall()]
        if not any(host == domain or host.endswith("." + domain) for domain in allowed):
            raise RuntimeError("Pinned browser tab is outside the enabled JobOS browser allowlist.")
        snapshot = transport.snapshot(target.target_id)
        if snapshot.get("truncated"):
            raise RuntimeError("Browser snapshot is truncated; open the complete form before preparing approval.")
        canonical_url = canonical_page_url(actual_url)
        fingerprint = page_fingerprint(snapshot, page_url=actual_url)
        fields = inspect_nodes(parse_snapshot(snapshot))
        groups = inspect_question_groups(parse_snapshot(snapshot))
        actions, _ = plan_autofill(fields, profile, question_groups=groups,
                                   approved_sensitive_answers=sensitive)
        writes = [item for item in actions if item.action in {"fill", "select", "check", "upload"}]
        pauses = [item.question_label or item.reason for item in actions if item.action == "pause"]
        summary = {
            "company": application[0], "role": application[1], "application_id": application_id,
            "pinned_target_id": target.target_id, "page_url": canonical_url,
            "page_fingerprint": fingerprint, "resume_artifact": document[2],
            "will_write": len(writes), "will_pause": pauses,
            "submit": "human_only",
        }
        print(json.dumps(summary, indent=2))
        if not create:
            return 0
        if not yes and input("Create approval for this exact tab and plan? [y/N] ").strip().casefold() not in {"y", "yes"}:
            print("No approval created.")
            return 0
        command = [
            sys.executable, str(ROOT / "services" / "approval" / "approval_service_v1.py"), "create",
            "--type", "autofill_form", "--application-id", application_id,
            "--document-id", document[0], "--expected-origin", _origin(canonical_url),
            "--expected-page-url", canonical_url, "--expected-page-fingerprint", fingerprint,
            "--apply",
        ]
        if document[1]:
            command.extend(("--artifact-id", document[1]))
        # Commit no DB work in this read phase. The approval service owns its
        # own transaction and token lifecycle.
        conn.rollback()
    return subprocess.call(command, cwd=ROOT)


def status() -> int:
    """Print the product-level pipeline state without invoking a worker or model."""
    import psycopg
    load_repo_env()
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT coalesce(status, 'unknown'), count(*) FROM applications GROUP BY 1 ORDER BY 1;")
        applications = {str(status): count for status, count in cur.fetchall()}
        cur.execute("SELECT coalesce(source, 'unknown'), count(*) FROM applications GROUP BY 1 ORDER BY 2 DESC;")
        sources = {str(source): count for source, count in cur.fetchall()}
        cur.execute("SELECT status, count(*) FROM browser_tasks GROUP BY 1 ORDER BY 1;")
        browser_tasks = {str(task_status): count for task_status, count in cur.fetchall()}
        cur.execute("SELECT count(*) FROM browser_tasks WHERE execution_state = 'needs_reconciliation';")
        reconciliation = int(cur.fetchone()[0])
        cur.execute("SELECT coalesce(status, 'unknown'), count(*) FROM application_attempts GROUP BY 1 ORDER BY 1;")
        attempts = {str(attempt_status): count for attempt_status, count in cur.fetchall()}
    print(json.dumps({
        "applications_by_status": applications, "applications_by_source": sources,
        "browser_tasks": browser_tasks, "attempts": attempts,
        "needs_reconciliation": reconciliation, "submit": "human_only",
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS operator commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_parser = commands.add_parser("doctor", help="Read-only readiness and safety checks.")
    doctor_parser.add_argument("--check-browser", action="store_true", help="Also probe gateway and CDP health; never opens a page.")
    autofill_parser = commands.add_parser("autofill", help="Prepare a deterministic, approval-bound form session.")
    autofill_subcommands = autofill_parser.add_subparsers(dest="autofill_command", required=True)
    prepare_parser = autofill_subcommands.add_parser("prepare", help="Inspect the pinned application tab and optionally create its exact approval.")
    prepare_parser.add_argument("--application-id", required=True)
    prepare_parser.add_argument("--create", action="store_true", help="After showing the plan, prompt to create a one-time approval.")
    prepare_parser.add_argument("--yes", action="store_true", help="Create without an interactive confirmation; use only after reviewing the printed plan.")
    commands.add_parser("status", help="Read-only product status: applications, browser tasks, and attempts.")
    args = parser.parse_args()
    if args.command == "doctor":
        return doctor(check_browser=args.check_browser)
    if args.command == "status":
        return status()
    return autofill_prepare(args.application_id, create=args.create, yes=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
