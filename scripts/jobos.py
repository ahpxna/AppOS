#!/usr/bin/env python3
"""Small operator entrypoint for safe JobOS readiness and browser preparation."""
from __future__ import annotations

import argparse
import hashlib
import os
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.common.config import database_dsn, load_repo_env
from services.common.autofill_identity import canonical_page_url, page_fingerprint
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
            mark(results, "PostgreSQL", True)
            cur.execute("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id = '061_telegram_review_channel.sql')")
            mark(results, "Migrations through 061", bool(cur.fetchone()[0]))
            cur.execute("SELECT to_regclass('public.autofill_action_journal') IS NOT NULL")
            mark(results, "Autofill action journal", bool(cur.fetchone()[0]))
            cur.execute("SELECT to_regclass('public.human_review_items') IS NOT NULL")
            mark(results, "Human Review Hub", bool(cur.fetchone()[0]))
            cur.execute("SELECT count(*) FROM browser_tasks WHERE execution_state = 'needs_reconciliation'")
            unresolved = int(cur.fetchone()[0])
            mark(results, "No unresolved autofill task", unresolved == 0, f"{unresolved} unresolved")
            cur.execute("SELECT count(*) FROM immigration_profiles WHERE profile_key = 'primary' AND user_confirmed_at IS NOT NULL")
            mark(results, "Immigration profile confirmed", int(cur.fetchone()[0]) == 1)
    except Exception as exc:
        mark(results, "PostgreSQL", False, str(exc)[:180])
        mark(results, "Migrations through 061", False, "PostgreSQL unavailable")

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
    checks = {name: ok for name, ok, _ in results}
    core = all(checks.get(name, False) for name in ("Python 3.11+", "Environment", "PostgreSQL", "Migrations through 061"))
    autofill = core and all(ok for name, ok, _ in results if name in {
        "Autofill action journal", "No unresolved autofill task", "Immigration profile confirmed",
        "OpenClaw runtime", "Managed upload root", "Human Review Hub",
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


def autofill_prepare(application_id: str, *, create: bool, yes: bool) -> int:
    """Pin the current tab and create an exact approval without user-supplied IDs.

    This is the only product-facing bridge into the deterministic autofill
    approval flow. It reads the tab, profile, and approved resume locally;
    no OpenClaw agent or LLM receives applicant data in this path.
    """
    load_repo_env()
    import psycopg
    from services.autofill.autofill_agent_v1 import parse_snapshot
    from services.autofill.autofill_context_v1 import load_autofill_context
    from services.autofill.autofill_executor_v1 import OpenClawTransport
    from services.autofill.autofill_planner_v1 import plan_autofill
    from services.autofill.form_inspector_v1 import inspect_nodes, inspect_question_groups
    from services.common.immigration_semantics import classify_immigration_question
    from services.common.question_memory import normalize_question
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT company, job_title, coalesce(ats_type, 'unknown') FROM applications WHERE id = %s;", (application_id,))
        application = cur.fetchone()
        if not application:
            raise RuntimeError("Application was not found.")
        cur.execute("""SELECT autofill_mode, supports_static_text, supports_radio,
                              supports_select, supports_upload
                       FROM ats_capabilities WHERE ats_type = %s;""", (application[2],))
        capability = cur.fetchone()
        if not capability or capability[0] == "review_only":
            raise RuntimeError(f"ATS '{application[2]}' is review-only or unregistered for deterministic autofill.")
        action_capabilities = {"fill": bool(capability[1]), "check": bool(capability[2]),
                               "select": bool(capability[3]), "upload": bool(capability[4])}
        cur.execute(
            """SELECT gd.id::text, gd.content, gda.id::text, gda.filename, gda.sha256
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
        artifact_binding = {"artifact_id": document[2], "artifact_filename": document[3],
                            "artifact_sha256": document[4]} if document[2] else {}
        context = load_autofill_context(
            cur, application_id=application_id, artifact_binding=artifact_binding,
            document_sha256=hashlib.sha256((document[1] or "").encode("utf-8")).hexdigest(),
            page_url=canonical_url, page_fingerprint_sha256=fingerprint,
            data_root=ROOT / "data",
        )
        fields = inspect_nodes(parse_snapshot(snapshot))
        groups = inspect_question_groups(parse_snapshot(snapshot))
        actions, _ = plan_autofill(fields, context.profile, question_groups=groups,
                                   approved_sensitive_answers=context.sensitive_answers,
                                   remembered_answers=context.remembered_answers)
        actions = [
            action if action.action not in action_capabilities or action_capabilities[action.action]
            else type(action)("pause", action.ref, None, action.profile_key,
                              f"ATS capability does not permit {action.action}.", action.question_label)
            for action in actions
        ]
        writes = [item for item in actions if item.action in {"fill", "select", "check", "upload"}]
        pauses = [item.question_label or item.reason for item in actions if item.action == "pause"]
        action_scope = {
            "profile_keys": sorted({item.profile_key for item in writes if item.profile_key and not item.profile_key.startswith("documents.")}),
            "document_types": sorted({item.profile_key.removeprefix("documents.") for item in writes if item.profile_key and item.profile_key.startswith("documents.")}),
            "sensitive_classes": sorted({kind.value for item in writes if item.question_label
                                         for kind in [classify_immigration_question(item.question_label)] if kind is not None}),
            "remembered_questions": sorted({normalize_question(item.question_label) for item in writes
                                            if item.profile_key is None and item.question_label and classify_immigration_question(item.question_label) is None}),
        }
        summary = {
            "company": application[0], "role": application[1], "application_id": application_id,
            "pinned_target_id": target.target_id, "page_url": canonical_url,
            "page_fingerprint": fingerprint, "resume_artifact": document[3], "resume_artifact_sha256": document[4],
            "will_write": len(writes),
            "write_actions": [{"action": item.action, "field": item.question_label, "profile_key": item.profile_key,
                               "value": item.value, "source": "confirmed_immigration" if item.question_label and classify_immigration_question(item.question_label) else "approved_profile"}
                              for item in writes],
            "action_scope": action_scope, "will_pause": pauses,
            "submit": "human_only",
        }
        print(json.dumps(summary, indent=2))
        if not create:
            return 0
        if not yes and input("Create approval for this exact tab and plan? [y/N] ").strip().casefold() not in {"y", "yes"}:
            print("No approval created.")
            return 0
        command = [
            sys.executable,
            str(ROOT / "services" / "approval" / "approval_service_v1.py"),
            "create",

            "--type",
            "autofill_form",

            "--application-id",
            application_id,

            "--document-id",
            document[0],

            "--expected-origin",
            _origin(canonical_url),

            "--expected-page-url",
            canonical_url,

            "--expected-page-fingerprint",
            fingerprint,

            "--expected-autofill-input-hash",
            context.input_hash,

            "--autofill-action-scope-json",
            json.dumps(action_scope, separators=(",", ":")),

            "--apply",
        ]
        if document[2]:
            command.extend(("--artifact-id", document[2]))
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
        cur.execute("SELECT count(*) FROM human_review_items WHERE status IN ('pending','needs_revision');")
        pending_reviews = int(cur.fetchone()[0])
    print(json.dumps({
        "applications_by_status": applications, "applications_by_source": sources,
        "browser_tasks": browser_tasks, "attempts": attempts, "pending_human_reviews": pending_reviews,
        "needs_reconciliation": reconciliation, "submit": "human_only",
    }, indent=2))
    return 0


def saved_sync(max_results: int, timeout: int) -> int:
    load_repo_env()
    command = [sys.executable, str(ROOT / "services" / "discovery" / "linkedin_intake_v1.py"),
               "queue-saved", "--max-results", str(max_results), "--timeout", str(timeout)]
    return subprocess.call(command, cwd=ROOT)


def review_command(command: str, item_id: str | None = None, note: str = "",
                   answer_text: str = "", answer_scope: str = "company") -> int:
    load_repo_env()
    argv = [sys.executable, str(ROOT / "services" / "review" / "review_service_v1.py"), command]
    if item_id:
        argv.append(item_id)
    if note and command in {"approve", "reject", "revise"}:
        argv.extend(("--note", note))
    if command == "answer":
        argv.extend(("--text", answer_text, "--scope", answer_scope))
    return subprocess.call(argv, cwd=ROOT)


def telegram_start(*, once: bool = False, dispatch_only: bool = False, discover_id: bool = False) -> int:
    load_repo_env()
    argv = [sys.executable, str(ROOT / "services" / "telegram" / "telegram_review_bot_v1.py")]
    if once:
        argv.append("--once")
    if dispatch_only:
        argv.append("--dispatch-only")
    if discover_id:
        argv.append("--discover-id")
    return subprocess.call(argv, cwd=ROOT)


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
    commands.add_parser("status", help="Read-only product status: applications, browser tasks, attempts, and human reviews.")

    saved_parser = commands.add_parser("saved", help="LinkedIn Saved Jobs read-only intake.")
    saved_sub = saved_parser.add_subparsers(dest="saved_command", required=True)
    saved = saved_sub.add_parser("sync", help="Queue a bounded Saved Jobs sync.")
    saved.add_argument("--limit", type=int, default=10)
    saved.add_argument("--timeout", type=int, default=600)

    review_parser = commands.add_parser("review", help="Unified Human Review Hub.")
    review_sub = review_parser.add_subparsers(dest="review_command", required=True)
    review_sub.add_parser("sync")
    review_sub.add_parser("inbox")
    show = review_sub.add_parser("show")
    show.add_argument("item_id")
    for decision in ("approve", "reject", "revise"):
        item = review_sub.add_parser(decision)
        item.add_argument("item_id")
        item.add_argument("--note", default="")
    answer = review_sub.add_parser("answer")
    answer.add_argument("item_id")
    answer.add_argument("--text", required=True)
    answer.add_argument("--scope", choices=("company", "ats", "global"), default="company")

    telegram_parser = commands.add_parser("telegram", help="Telegram remote review adapter.")
    telegram_sub = telegram_parser.add_subparsers(dest="telegram_command", required=True)
    start = telegram_sub.add_parser("start")
    start.add_argument("--once", action="store_true")
    start.add_argument("--dispatch-only", action="store_true")
    telegram_sub.add_parser("discover-id", help="Print recent Telegram user/chat ids after you send /start to the bot.")
    args = parser.parse_args()
    if args.command == "doctor":
        return doctor(check_browser=args.check_browser)
    if args.command == "status":
        return status()
    if args.command == "saved":
        return saved_sync(args.limit, args.timeout)
    if args.command == "review":
        return review_command(args.review_command, getattr(args, "item_id", None),
                              getattr(args, "note", ""), getattr(args, "text", ""),
                              getattr(args, "scope", "company"))
    if args.command == "telegram":
        if args.telegram_command == "discover-id":
            return telegram_start(discover_id=True)
        return telegram_start(once=args.once, dispatch_only=args.dispatch_only)
    return autofill_prepare(args.application_id, create=args.create, yes=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
