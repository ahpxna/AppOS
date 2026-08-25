#!/usr/bin/env python3
"""Small operator entrypoint for safe JobOS readiness and browser preparation."""
from __future__ import annotations

import argparse
import hashlib
import os
import json
import subprocess
import sys
import shutil
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

    gog = shutil.which((os.getenv("JOBOS_GOG_BIN") or "gog").strip())
    mark(results, "Gmail gog reader", bool(gog), gog or "install/authenticate gog before email verification")
    vault_path = Path(os.getenv("JOBOS_VAULT_KEY_FILE", str(ROOT / "data" / "secrets" / "jobos-vault.key"))).expanduser()
    mark(results, "Credential vault key", vault_path.is_file(), str(vault_path))
    tg = bool((os.getenv("JOBOS_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
              and (os.getenv("JOBOS_TELEGRAM_ALLOWED_USER_ID") or os.getenv("TELEGRAM_ALLOWED_USER_ID") or "").strip())
    mark(results, "Telegram approval channel", tg, "configured" if tg else "set Telegram bot + allowed user id")

    try:
        import psycopg
        with psycopg.connect(database_dsn(), connect_timeout=5) as conn, conn.cursor() as cur:
            mark(results, "PostgreSQL", True)
            cur.execute("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id = '071_human_approval_bus_and_privileged_actions.sql')")
            mark(results, "Migrations through 071", bool(cur.fetchone()[0]))
            cur.execute("SELECT to_regclass('public.privileged_action_executions') IS NOT NULL")
            mark(results, "Human Approval Bus", bool(cur.fetchone()[0]))
            cur.execute("SELECT to_regclass('public.autofill_action_journal') IS NOT NULL")
            mark(results, "Autofill action journal", bool(cur.fetchone()[0]))
            cur.execute("SELECT to_regclass('public.human_review_items') IS NOT NULL")
            mark(results, "Human Review Hub", bool(cur.fetchone()[0]))
            cur.execute("""SELECT
                           (SELECT count(*) FROM browser_tasks WHERE execution_state = 'needs_reconciliation') +
                           (SELECT count(*) FROM privileged_action_executions WHERE status = 'needs_reconciliation')""")
            unresolved = int(cur.fetchone()[0])
            mark(results, "No unresolved browser action", unresolved == 0, f"{unresolved} unresolved")
            cur.execute("SELECT count(*) FROM immigration_profiles WHERE profile_key = 'primary' AND user_confirmed_at IS NOT NULL")
            mark(results, "Immigration profile confirmed", int(cur.fetchone()[0]) == 1)
    except Exception as exc:
        mark(results, "PostgreSQL", False, str(exc)[:180])
        mark(results, "Migrations through 071", False, "PostgreSQL unavailable")

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
    core = all(checks.get(name, False) for name in ("Python 3.11+", "Environment", "PostgreSQL", "Migrations through 071"))
    autofill = core and all(ok for name, ok, _ in results if name in {
        "Autofill action journal", "No unresolved autofill task", "Immigration profile confirmed",
        "OpenClaw runtime", "Managed upload root", "Human Review Hub", "Human Approval Bus",
    })
    print(f"\nCORE READY: {'YES' if core else 'NO'}")
    print(f"AUTOFILL READY: {'YES' if autofill else 'NO'}")
    print("SUBMIT: TELEGRAM HUMAN APPROVAL + PRIVILEGED ONE-SHOT EXECUTOR ONLY")
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
    from services.common.autofill_action_scope import build_exact_action_scope
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT company, job_title, coalesce(ats_type, 'unknown'),
                      approved_resume_id::text, approved_resume_artifact_id::text
                 FROM applications WHERE id = %s;""",
            (application_id,),
        )
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
                 FROM applications a
                 JOIN generated_documents gd ON gd.id = a.approved_resume_id
                 JOIN generated_document_artifacts gda ON gda.id = a.approved_resume_artifact_id
                WHERE a.id = %s AND gd.application_id = a.id AND gd.doc_type = 'resume'
                  AND gd.qa_status = 'pass' AND gd.approved = true
                  AND gda.application_id = a.id AND gda.generated_document_id = gd.id;""",
            (application_id,),
        )
        document = cur.fetchone()
        if not document:
            raise RuntimeError("No exact reviewed resume PDF artifact is approved for this application.")
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
        writes = [item for item in actions if item.action in {"fill", "select", "check"}]
        upload_actions = [item for item in actions if item.action == "upload"]
        pauses = [item.question_label or item.reason for item in actions if item.action == "pause"]
        action_scope = build_exact_action_scope(writes)
        # Human-facing summary keys remain useful, but authorization is the
        # exact action list in build_exact_action_scope().
        action_scope["sensitive_classes"] = sorted({kind.value for item in writes if item.question_label
                                                    for kind in [classify_immigration_question(item.question_label)] if kind is not None})
        action_scope["remembered_questions"] = sorted({normalize_question(item.question_label) for item in writes
                                                       if item.profile_key is None and item.question_label and classify_immigration_question(item.question_label) is None})
        upload_packages = []
        for item in upload_actions:
            doc_type = str(item.profile_key or "").removeprefix("documents.")
            if doc_type not in {"resume", "cover_letter"}:
                continue
            pointer_cols = ("approved_resume_id", "approved_resume_artifact_id") if doc_type == "resume" else ("approved_cover_letter_id", "approved_cover_letter_artifact_id")
            cur.execute(
                f"""SELECT gd.id::text, gda.id::text, gda.file_path, gda.filename, gda.sha256,
                           gd.source_jd_hash, a.jd_hash
                      FROM applications a
                      JOIN generated_documents gd ON gd.id = a.{pointer_cols[0]}
                      JOIN generated_document_artifacts gda ON gda.id = a.{pointer_cols[1]}
                     WHERE a.id=%s AND gd.application_id=a.id AND gda.application_id=a.id
                       AND gda.generated_document_id=gd.id AND gd.doc_type=%s
                       AND gd.qa_status='pass' AND gd.approved=true;""",
                (application_id, doc_type),
            )
            bound = cur.fetchone()
            if not bound or not bound[5] or str(bound[5]) != str(bound[6]):
                pauses.append(f"{item.question_label or doc_type}: approved document is stale against the current JD")
                continue
            upload_packages.append({
                "target_id": target.target_id, "expected_url": canonical_url,
                "expected_page_fingerprint": fingerprint, "expected_origin": _origin(canonical_url),
                "field_ref": item.ref, "field_label": item.question_label or "",
                "document_type": doc_type, "generated_document_id": bound[0], "artifact_id": bound[1],
                "file_path": bound[2], "filename": bound[3], "sha256": bound[4],
                "source_jd_hash": str(bound[5]), "application_jd_hash": str(bound[6]),
                "review_context": {"screenshot_path": "NaN", "upload": {"field": item.question_label or item.ref, "document_type": doc_type, "filename": bound[3], "sha256": bound[4]}},
            })
        summary = {
            "company": application[0], "role": application[1], "application_id": application_id,
            "pinned_target_id": target.target_id, "page_url": canonical_url,
            "page_fingerprint": fingerprint, "resume_artifact": document[3], "resume_artifact_sha256": document[4],
            "will_write": len(writes),
            "write_actions": [{"action": item.action, "field": item.question_label, "ref": item.ref, "profile_key": item.profile_key,
                               "value": item.value, "source": "confirmed_immigration" if item.question_label and classify_immigration_question(item.question_label) else "approved_profile"}
                              for item in writes],
            "separate_upload_approvals": [{"field": pkg["field_label"], "ref": pkg["field_ref"],
                                            "document_type": pkg["document_type"], "filename": pkg["filename"],
                                            "sha256": pkg["sha256"]} for pkg in upload_packages],
            "action_scope": action_scope, "will_pause": pauses,
            "submit": "telegram_human_approval_required",
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

            "--review-context-json",
            json.dumps({
                "write_actions": summary["write_actions"],
                "will_pause": summary["will_pause"],
                "uploads": summary["separate_upload_approvals"],
                "pinned_target_id": summary["pinned_target_id"],
                "page_url": summary["page_url"],
                "page_fingerprint": summary["page_fingerprint"],
                "resume_artifact": summary["resume_artifact"],
                "resume_artifact_sha256": summary["resume_artifact_sha256"],
            }, separators=(",", ":")),

            "--apply",
        ]
        if document[2]:
            command.extend(("--artifact-id", document[2]))
        # Commit no DB work in this read phase. The approval service owns its
        # own transaction and token lifecycle. Child upload capabilities are
        # materialized only after the parent approval creation succeeds.
        pending_upload_packages = list(upload_packages)
        conn.rollback()
    rc = subprocess.call(command, cwd=ROOT)
    if rc == 0 and pending_upload_packages:
        from services.application_actions.action_request_v1 import create_privileged_request
        with psycopg.connect(database_dsn(), autocommit=False) as upload_conn, upload_conn.cursor() as upload_cur:
            created_uploads = []
            for package in pending_upload_packages:
                rid = create_privileged_request(
                    upload_cur, application_id=application_id, action_type="privileged_upload_document",
                    payload=package,
                    summary=f"Upload exact approved {package['document_type']} {package['filename']!r} to field {package['field_label'] or package['field_ref']!r}.",
                    requested_by="autofill-prepare",
                )
                created_uploads.append(rid)
            upload_conn.commit()
        print(json.dumps({"separate_upload_approval_requests": created_uploads}, indent=2))
    return rc


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
        autofill_reconciliation = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM privileged_action_executions WHERE status = 'needs_reconciliation';")
        privileged_reconciliation = int(cur.fetchone()[0])
        reconciliation = autofill_reconciliation + privileged_reconciliation
        cur.execute("SELECT coalesce(status, 'unknown'), count(*) FROM application_attempts GROUP BY 1 ORDER BY 1;")
        attempts = {str(attempt_status): count for attempt_status, count in cur.fetchall()}
        cur.execute("SELECT count(*) FROM human_review_items WHERE status IN ('pending','needs_revision');")
        pending_reviews = int(cur.fetchone()[0])
    print(json.dumps({
        "applications_by_status": applications, "applications_by_source": sources,
        "browser_tasks": browser_tasks, "attempts": attempts, "pending_human_reviews": pending_reviews,
        "needs_reconciliation": reconciliation,
        "autofill_needs_reconciliation": autofill_reconciliation,
        "privileged_needs_reconciliation": privileged_reconciliation,
        "submit": "telegram_human_approval_then_privileged_executor",
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



def action_command(command: str, *, application_id: str = "", action: str = "",
                   candidate_id: str = "", request_id: str = "", poll_seconds: int = 5) -> int:
    load_repo_env()
    argv = [sys.executable, str(ROOT / "services" / "application_actions" / "privileged_action_v1.py"), command]
    if command == "prepare":
        argv.extend(("--application-id", application_id, "--action", action))
        if candidate_id:
            argv.extend(("--candidate-id", candidate_id))
    elif command == "execute" and request_id:
        argv.extend(("--request-id", request_id))
    elif command == "worker":
        argv.extend(("--poll-seconds", str(poll_seconds)))
    return subprocess.call(argv, cwd=ROOT)


def vault_command(command: str, *, origin: str = "", account: str = "", kind: str = "password", length: int = 28) -> int:
    load_repo_env()
    argv = [sys.executable, str(ROOT / "services" / "security" / "credential_vault_v1.py"), command]
    if command != "init":
        argv.extend(("--origin", origin, "--account", account, "--kind", kind))
    if command == "generate":
        argv.extend(("--length", str(length)))
    return subprocess.call(argv, cwd=ROOT)


def gmail_verify_command(*, application_id: str, recipient: str, employer_origin: str = "",
                         since_seconds: int = 300, max_results: int = 10) -> int:
    load_repo_env()
    argv = [sys.executable, str(ROOT / "services" / "auth" / "gmail_verification_v1.py"),
            "--application-id", application_id, "--recipient", recipient,
            "--since-unix", str(__import__('time').time() - max(1, since_seconds)),
            "--max-results", str(max_results)]
    if employer_origin:
        argv.extend(("--employer-origin", employer_origin))
    return subprocess.call(argv, cwd=ROOT)


def gmail_watch_command(*, once: bool = False, wake_listen: bool = False,
                        interval_seconds: int = 10, max_results: int = 10,
                        wake_host: str = "127.0.0.1", wake_port: int = 8791) -> int:
    load_repo_env()
    argv = [sys.executable, str(ROOT / "services" / "auth" / "gmail_verification_watcher_v1.py"),
            "--interval-seconds", str(interval_seconds), "--max-results", str(max_results)]
    if once:
        argv.append("--once")
    if wake_listen:
        argv.extend(("--wake-listen", "--wake-host", wake_host, "--wake-port", str(wake_port)))
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

    action_parser = commands.add_parser("action", help="Prepare/execute human-approved privileged application actions.")
    action_sub = action_parser.add_subparsers(dest="action_command", required=True)
    ap = action_sub.add_parser("prepare")
    ap.add_argument("--application-id", required=True)
    ap.add_argument("--action", required=True, choices=("begin_application","trust_external_domain","create_employer_account","login_employer_account","use_email_verification","accept_terms","advance_application_step","auth_manual_retry","mfa_retry","checkpoint_retry","submit_application"))
    ap.add_argument("--candidate-id")
    ae = action_sub.add_parser("execute"); ae.add_argument("--request-id", required=True)
    action_sub.add_parser("once", help="Execute the next approved privileged action exactly once.")
    aw = action_sub.add_parser("worker", help="Continuously execute Telegram-approved privileged actions one at a time.")
    aw.add_argument("--poll-seconds", type=int, default=5)

    vault_parser = commands.add_parser("vault", help="Encrypted employer credential vault.")
    vault_sub = vault_parser.add_subparsers(dest="vault_command", required=True)
    vault_sub.add_parser("init")
    for name in ("set", "generate", "status", "revoke"):
        vp = vault_sub.add_parser(name); vp.add_argument("--origin", required=True); vp.add_argument("--account", required=True); vp.add_argument("--kind", default="password")
        if name == "generate": vp.add_argument("--length", type=int, default=28)

    gmail_parser = commands.add_parser("gmail", help="Bounded Gmail verification reader (Inbox/other labels + Spam).")
    gmail_sub = gmail_parser.add_subparsers(dest="gmail_command", required=True)
    gv = gmail_sub.add_parser("verify")
    gv.add_argument("--application-id", required=True); gv.add_argument("--recipient", required=True)
    gv.add_argument("--employer-origin", default=""); gv.add_argument("--since-seconds", type=int, default=300); gv.add_argument("--max-results", type=int, default=10)
    gw = gmail_sub.add_parser("watch")
    gw.add_argument("--once", action="store_true"); gw.add_argument("--wake-listen", action="store_true")
    gw.add_argument("--wake-host", default="127.0.0.1"); gw.add_argument("--wake-port", type=int, default=8791)
    gw.add_argument("--interval-seconds", type=int, default=10); gw.add_argument("--max-results", type=int, default=10)

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
    if args.command == "action":
        return action_command(args.action_command, application_id=getattr(args, "application_id", ""),
                              action=getattr(args, "action", ""), candidate_id=getattr(args, "candidate_id", "") or "",
                              request_id=getattr(args, "request_id", ""), poll_seconds=getattr(args, "poll_seconds", 5))
    if args.command == "vault":
        return vault_command(args.vault_command, origin=getattr(args, "origin", ""), account=getattr(args, "account", ""),
                             kind=getattr(args, "kind", "password"), length=getattr(args, "length", 28))
    if args.command == "gmail":
        if args.gmail_command == "watch":
            return gmail_watch_command(once=args.once, wake_listen=args.wake_listen,
                                       wake_host=args.wake_host, wake_port=args.wake_port,
                                       interval_seconds=args.interval_seconds, max_results=args.max_results)
        return gmail_verify_command(application_id=args.application_id, recipient=args.recipient,
                                    employer_origin=args.employer_origin, since_seconds=args.since_seconds, max_results=args.max_results)
    return autofill_prepare(args.application_id, create=args.create, yes=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
