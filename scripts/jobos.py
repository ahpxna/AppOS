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
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_PROFILE = "v0.1.0"
sys.path.insert(0, str(ROOT))

from services.common.config import database_dsn, load_repo_env
from services.common.autofill_identity import canonical_page_url, page_fingerprint
from services.common.openclaw_runtime import (
    find_global_openclaw_conflicts, inspect_global_openclaw_install,
    remove_proven_global_openclaw, resolve_openclaw_binary,
)
from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER


def mark(results: list[tuple[str, bool, str]], name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def _latest_migration_contract() -> tuple[str, str, str]:
    migrations = sorted((ROOT / "db" / "migrations").glob("*.sql"), key=lambda path: path.name)
    if not migrations:
        raise RuntimeError("No SQL migrations are present.")
    latest = migrations[-1]
    prefix = latest.name.split("_", 1)[0]
    return latest.name, prefix, hashlib.sha256(latest.read_bytes()).hexdigest()



def _resolve_global_openclaw_conflicts_interactively() -> tuple[list[Path], bool, str]:
    """Offer provenance-safe global cleanup; never delete an unknown binary.

    Returns ``(remaining_conflicts, interrupted, detail)``.  Non-interactive
    doctor runs never mutate the host and therefore remain fail-closed.
    """
    conflicts = find_global_openclaw_conflicts()
    if not conflicts:
        return [], False, "none"
    installs = [inspect_global_openclaw_install(path) for path in conflicts]
    detail = "; ".join(
        f"{item.path} [{item.provenance}{', removable' if item.removable else ', manual removal required'}]"
        for item in installs
    )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return conflicts, False, detail + "; non-interactive doctor will not mutate the host"

    print("\nGlobal OpenClaw installation(s) detected outside JobOS managed Node runtime:")
    for item in installs:
        print(f"  - {item.path} — {item.provenance}" + ("" if item.removable else " — cannot auto-remove safely"))
    try:
        answer = input("Remove package-manager-proven global OpenClaw installation(s)? [y/N] ").strip().casefold()
    except (KeyboardInterrupt, EOFError):
        print("\nGlobal OpenClaw cleanup cancelled; no removal command was started.")
        return conflicts, True, detail
    if answer not in {"y", "yes"}:
        return conflicts, False, detail

    errors: list[str] = []
    for item in installs:
        if not item.removable:
            errors.append(f"{item.path}: unknown provenance; remove manually")
            continue
        try:
            removed = remove_proven_global_openclaw(item.path)
            print(f"Removed {removed.path} via {removed.provenance}.")
        except RuntimeError as exc:
            errors.append(str(exc))
    remaining = find_global_openclaw_conflicts()
    return remaining, False, ("; ".join(errors) if errors else ("none" if not remaining else ", ".join(map(str, remaining))))

def doctor(*, check_browser: bool, strict: bool = False, require_autofill: bool = False, profile: str = "production") -> int:
    """Report readiness; only explicit ``y`` may remove a proven global OpenClaw package."""
    load_repo_env()
    results: list[tuple[str, bool, str]] = []
    mark(results, "Python 3.11+", sys.version_info >= (3, 11), sys.version.split()[0])
    env_path = ROOT / ".env"
    mark(results, "Environment", env_path.is_file(), str(env_path) if env_path.is_file() else "run bootstrap")
    template = Path(os.getenv("JOBOS_RESUME_TEMPLATE_PATH", str(ROOT / "data" / "resume-template" / "VU PHAN AN NGUYEN-official_For_all.docx"))).expanduser()
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
    global_conflicts, cleanup_interrupted, global_detail = _resolve_global_openclaw_conflicts_interactively()
    if cleanup_interrupted:
        return 130
    mark(
        results, "No global OpenClaw conflict", not global_conflicts, global_detail,
    )

    gog = shutil.which((os.getenv("JOBOS_GOG_BIN") or "gog").strip())
    mark(results, "Gmail gog reader", bool(gog), gog or "install/authenticate gog before email verification")
    vault_path = Path(os.getenv("JOBOS_VAULT_KEY_FILE", str(ROOT / "data" / "secrets" / "jobos-vault.key"))).expanduser()
    mark(results, "Credential vault key", vault_path.is_file(), str(vault_path))
    tg = bool((os.getenv("JOBOS_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
              and (os.getenv("JOBOS_TELEGRAM_ALLOWED_USER_ID") or os.getenv("TELEGRAM_ALLOWED_USER_ID") or "").strip())
    mark(results, "Telegram approval channel", tg, "configured" if tg else "set Telegram bot + allowed user id")

    try:
        latest_migration, latest_number, latest_checksum = _latest_migration_contract()
    except Exception as exc:
        latest_migration, latest_number, latest_checksum = "unknown", "?", ""
        mark(results, "Migration files", False, str(exc))
    migration_check_name = f"Migrations through {latest_number}"

    try:
        import psycopg
        with psycopg.connect(database_dsn(), connect_timeout=5) as conn, conn.cursor() as cur:
            mark(results, "PostgreSQL", True)
            cur.execute("SELECT checksum_sha256 FROM schema_migrations WHERE migration_id=%s", (latest_migration,))
            migration_row = cur.fetchone()
            migration_ok = bool(migration_row and str(migration_row[0]) == latest_checksum)
            mark(results, migration_check_name, migration_ok,
                 latest_migration if migration_ok else f"missing/checksum drift: {latest_migration}")
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
        mark(results, migration_check_name, False, "PostgreSQL unavailable")

    if check_browser:
        # Health only: it validates gateway/CDP availability but does not list
        # tabs, read a page, run an agent, or issue a browser write.
        result = DEFAULT_PROCESS_RUNNER.run(
            [sys.executable, str(ROOT / "services" / "browser-controller" / "browser_queue_worker.py"), "--health"],
            cwd=ROOT, timeout_s=30,
        )
        detail = (result.output + (f" {result.start_error}" if result.start_error else "")).strip()
        mark(results, "Gateway and CDP health", result.ok, detail.replace("\n", " ")[:180])

    print("JOBOS DOCTOR\n")
    for name, ok, detail in results:
        print(f"{'✓' if ok else '⚠'} {name}" + (f" — {detail}" if detail else ""))
    checks = {name: ok for name, ok, _ in results}
    core = all(checks.get(name, False) for name in ("Python 3.11+", "Environment", "PostgreSQL", migration_check_name))
    document_ready = core and checks.get("Resume template", False)
    form_fill_ready = core and all(checks.get(name, False) for name in (
        "Autofill action journal", "No unresolved browser action", "Immigration profile confirmed",
        "OpenClaw runtime", "No global OpenClaw conflict", "Managed upload root", "Human Review Hub", "Human Approval Bus",
    ))
    auth_flow_ready = form_fill_ready and checks.get("Credential vault key", False)
    privileged_ready = form_fill_ready and checks.get("Telegram approval channel", False)
    email_verification_ready = auth_flow_ready and checks.get("Gmail gog reader", False) and checks.get("Telegram approval channel", False)
    submit_ready = privileged_ready and checks.get("No unresolved browser action", False)
    readiness = {
        "CORE READY": core,
        "DOCUMENT READY": document_ready,
        "FORM-FILL READY": form_fill_ready,
        "AUTH FLOW READY": auth_flow_ready,
        "PRIVILEGED ACTION READY": privileged_ready,
        "EMAIL VERIFICATION READY": email_verification_ready,
        "SUBMIT READY": submit_ready,
    }
    for label, ok in readiness.items():
        print(f"{label}: {'YES' if ok else 'NO'}")
    print("SUBMIT: TELEGRAM HUMAN APPROVAL + PRIVILEGED ONE-SHOT EXECUTOR ONLY")
    profile_targets = {
        "core": core,
        "documents": document_ready,
        "browser": form_fill_ready and (checks.get("Gateway and CDP health", False) if check_browser else True),
        "production": all(readiness.values()) and (checks.get("Gateway and CDP health", False) if check_browser else True),
    }
    if profile not in profile_targets:
        raise RuntimeError(f"Unknown doctor profile: {profile}")
    print(f"DOCTOR PROFILE: {profile}")
    if strict:
        return 0 if profile_targets[profile] else 1
    if require_autofill:
        return 0 if form_fill_ready else 1
    return 0 if profile_targets[profile] else 1


def verify_release(*, profile: str) -> int:
    """Fail closed on every v0.1.0 release contract; never silently skip live gates."""
    if profile != RELEASE_PROFILE:
        raise RuntimeError(f"Unsupported release profile: {profile}")
    load_repo_env()
    db_enabled = os.getenv("JOBOS_RUN_DB_INTEGRATION") == "1" and bool(os.getenv("JOBOS_TEST_DSN", "").strip())
    stages: list[tuple[str, list[str], int]] = [
        ("compile/import", [sys.executable, "-m", "compileall", "-q", "."], 180),
        ("non-DB regression", [sys.executable, "-m", "pytest", "-q", "--ignore=tests/integration"], 900),
        ("migration lint", [sys.executable, "scripts/migration_lint.py"], 120),
        ("fresh DB lifecycle", [sys.executable, "-m", "pytest", "-q",
                                 "tests/integration/test_autofill_execution_lifecycle.py",
                                 "tests/integration/test_human_review_hub.py",
                                 "test_autofill_execution_db_integration.py"], 900),
        ("frozen-feature invariants", [sys.executable, "-m", "pytest", "-q",
                                        "tests/test_runtime_lifecycle_hardening_v29_combined.py",
                                        "tests/test_final_pipeline_integrity_v45.py"], 300),
    ]
    git_checkout = (ROOT / ".git").exists()
    manifest_file = ROOT / "RELEASE_MANIFEST.json"
    if git_checkout:
        stages.append(("release diff check", ["git", "diff", "--check"], 60))
    elif manifest_file.is_file():
        stages.append(("packaged release manifest", [sys.executable, "scripts/build_release.py",
                                                       "--verify-manifest", str(ROOT)], 120))
    else:
        print("RELEASE BLOCKED: run from a Git checkout or a packaged release with RELEASE_MANIFEST.json.")
        return 2
    if not db_enabled:
        print("RELEASE BLOCKED: set JOBOS_RUN_DB_INTEGRATION=1 and JOBOS_TEST_DSN to a disposable database.")
        return 2
    stages.append(("controlled CDP fixture", [sys.executable, "scripts/release_cdp_smoke.py"], 300))
    for name, argv, timeout_s in stages:
        result = DEFAULT_PROCESS_RUNNER.run(argv, cwd=ROOT, timeout_s=timeout_s)
        if not result.ok:
            detail = (result.output + (f"\n{result.start_error}" if result.start_error else "")).strip()
            print(f"RELEASE BLOCKED at {name}:\n{detail[-4000:]}")
            return 1
        print(f"✓ {name}")
    if git_checkout:
        status = DEFAULT_PROCESS_RUNNER.run(["git", "status", "--porcelain"], cwd=ROOT, timeout_s=30)
        if not status.ok or status.stdout.strip():
            print("RELEASE BLOCKED: release tree is not clean.")
            return 1
    print("V0.1.0 RELEASE VERIFICATION: PASS")
    return 0


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
    from services.common.autofill_action_scope import build_exact_action_scope, autofill_plan_key
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT company, job_title, coalesce(ats_type, 'unknown'),
                      approved_resume_id::text, approved_resume_artifact_id::text,
                      current_step, coalesce(job_url,''), coalesce(jd_hash,'')
                 FROM applications WHERE id = %s;""",
            (application_id,),
        )
        application = cur.fetchone()
        if not application:
            raise RuntimeError("Application was not found.")
        if str(application[5] or "") != "application_form_ready":
            raise RuntimeError(
                f"Autofill can only be prepared from application_form_ready; current step is {application[5]!r}. "
                "Resolve/close any prior approval or reconciliation first."
            )
        cur.execute("""SELECT autofill_mode, supports_static_text, supports_radio,
                              supports_select, supports_upload, verification_level
                       FROM ats_capabilities WHERE ats_type = %s;""", (application[2],))
        capability = cur.fetchone()
        verification = str(capability[5]) if capability else "review_only"
        if (not capability or capability[0] == "review_only" or
                verification not in {"generic_accessibility", "fixture_verified", "live_verified"}):
            raise RuntimeError(
                f"ATS '{application[2]}' is review-only, unregistered, or lacks an approved browser verification level."
            )
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
        from services.common.application_browser_binding_v1 import (
            ApplicationBrowserBindingError, resolve_application_bound_target,
        )
        try:
            target = resolve_application_bound_target(
                cur, transport, application_id=application_id, allow_focused_rebind=False
            )
        except ApplicationBrowserBindingError as exc:
            raise RuntimeError(str(exc)) from exc
        actual_url = transport.current_url(target.target_id)
        from services.common.domain_authority_v1 import host_is_authorized
        if not host_is_authorized(cur, actual_url, application_id=application_id,
                                  purpose="employer_handoff"):
            raise RuntimeError("Pinned browser tab is outside the active global or application-scoped employer trust.")
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
        action_scope = build_exact_action_scope([*writes, *upload_actions])
        # Human-facing summary keys remain useful, but authorization is the
        # exact action list in build_exact_action_scope().
        action_scope["sensitive_classes"] = sorted({kind.value for item in writes if item.question_label
                                                    for kind in [classify_immigration_question(item.question_label)] if kind is not None})
        action_scope["remembered_questions"] = sorted({normalize_question(item.question_label) for item in writes
                                                       if item.profile_key is None and item.question_label and classify_immigration_question(item.question_label) is None})
        plan_key = autofill_plan_key(
            application_id=application_id, page_url=canonical_url,
            page_fingerprint=fingerprint, input_hash=context.input_hash,
            action_scope=action_scope,
        )
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
                "autofill_plan_key": plan_key, "delegated_to_autofill": True,
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
            "action_scope": action_scope, "autofill_plan_key": plan_key, "will_pause": pauses,
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

            "--expected-target-id",
            target.target_id,

            "--expected-page-url",
            canonical_url,

            "--expected-page-fingerprint",
            fingerprint,

            "--expected-autofill-input-hash",
            context.input_hash,

            "--autofill-action-scope-json",
            json.dumps(action_scope, separators=(",", ":")),

            "--autofill-plan-key",
            plan_key,

            "--expected-upload-capabilities-json",
            json.dumps([
                {"field_ref": pkg["field_ref"], "document_type": pkg["document_type"],
                 "artifact_id": pkg["artifact_id"], "sha256": pkg["sha256"]}
                for pkg in upload_packages
            ], separators=(",", ":")),

            "--delegated-upload-packages-json",
            json.dumps(upload_packages, separators=(",", ":")),

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
        # Deterministic repair closes the parent-commit/child-create crash window.
        # It also binds every delegated child to the exact parent approval id.
        from services.approval.approval_service_v1 import queue_ready_autofill_for_plan
        with psycopg.connect(database_dsn(), autocommit=False) as upload_conn, upload_conn.cursor() as upload_cur:
            queue_ready_autofill_for_plan(
                upload_cur, application_id=application_id, plan_key=plan_key, actor="autofill-prepare",
            )
            upload_cur.execute(
                """SELECT id::text FROM approval_requests
                     WHERE application_id=%s AND type='autofill_form'
                       AND bound_autofill_plan_key=%s
                     ORDER BY created_at DESC LIMIT 1;""",
                (application_id, plan_key),
            )
            parent_row = upload_cur.fetchone()
            parent_request_id = str(parent_row[0]) if parent_row else ""
            upload_cur.execute(
                """SELECT id::text FROM approval_requests
                     WHERE application_id=%s AND type='privileged_upload_document'
                       AND parent_approval_request_id=%s::uuid
                     ORDER BY created_at;""",
                (application_id, parent_request_id),
            )
            created_uploads = [str(row[0]) for row in upload_cur.fetchall()]
            upload_conn.commit()
        print(json.dumps({"parent_approval_request_id": parent_request_id,
                          "separate_upload_approval_requests": created_uploads}, indent=2))
    return rc


def status() -> int:
    """Print runtime + pipeline state even when one subsystem is down."""
    import psycopg
    load_repo_env()
    runtime = {"running": False}

    def pid_alive(value: object) -> bool:
        try:
            os.kill(int(value), 0)
            return True
        except (TypeError, ValueError, OSError):
            return False
    try:
        runtime_path = ROOT / ".jobos" / "run" / "runtime.json"
        if runtime_path.is_file():
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            pid_path = ROOT / ".jobos" / "run" / "supervisor.pid"
            pid = None
            try:
                pid = int(pid_path.read_text(encoding="utf-8").strip())
            except Exception:
                pid = runtime.get("supervisor_pid")
            runtime["supervisor_pid"] = pid
            runtime["running"] = pid_alive(pid)
            services = runtime.get("services") if isinstance(runtime.get("services"), dict) else {}
            fallback_required = {"orchestrator", "privileged-actions", "browser-worker",
                                 "browser-state-watcher", "document-revision"}
            declared_required = {
                name for name, service in services.items()
                if isinstance(service, dict) and service.get("required")
            }
            advertised_required = runtime.get("expected_required_services")
            expected_required = (set(advertised_required) if isinstance(advertised_required, list)
                                 else set(fallback_required)) | declared_required
            try:
                runtime_age = max(0, int(time.time()) - int(runtime.get("updated_at_unix") or 0))
            except Exception:
                runtime_age = 10**9
            runtime["state_age_seconds"] = runtime_age
            runtime["state_fresh"] = bool(runtime.get("updated_at_unix")) and runtime_age <= 15
            runtime["required_failures"] = sorted(
                name for name in expected_required
                if not isinstance(services.get(name), dict) or not services[name].get("running")
            )
            runtime["ready"] = runtime["running"] and runtime["state_fresh"] and not runtime["required_failures"]
            if not runtime["running"]:
                runtime["status"] = "stale_runtime_state"
    except Exception:
        runtime = {"running": False, "status": "unavailable"}
    try:
        conn_ctx = psycopg.connect(database_dsn(), autocommit=True)
    except Exception as exc:
        print(json.dumps({
            "runtime": runtime,
            "database": {"ready": False, "error": str(exc)[:500]},
            "daily_control_surface": "Telegram /start",
        }, indent=2))
        return 1
    with conn_ctx as conn, conn.cursor() as cur:
        cur.execute("SELECT coalesce(status, 'unknown'), count(*) FROM applications GROUP BY 1 ORDER BY 1;")
        applications = {str(status): count for status, count in cur.fetchall()}
        cur.execute("SELECT coalesce(current_step, 'unknown'), count(*) FROM applications GROUP BY 1 ORDER BY 1;")
        steps = {str(step): count for step, count in cur.fetchall()}
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
        cur.execute(
            """SELECT a.id::text,a.company,a.job_title,a.current_step,
                      extract(epoch from (now()-a.updated_at))::bigint AS age_seconds,
                      a.processing_run_id::text,a.processing_lease_expires_at,a.last_error,
                      CASE
                        WHEN a.current_step='docs_verified' THEN 'human: approve reviewed resume / bind OPEN APPLY'
                        WHEN a.current_step IN ('needs_account_auth','needs_email_verification','needs_mfa','needs_human_checkpoint') THEN 'human/review: complete current auth gate'
                        WHEN a.current_step='application_form_ready' THEN 'review: prepare exact autofill'
                        WHEN a.current_step='awaiting_approval' THEN 'human: decide exact autofill/upload approvals'
                        WHEN a.current_step='autofill_executing' THEN 'worker lease active; do not replay'
                        WHEN a.current_step='application_ready' THEN 'human: prepare next/submit gate'
                        ELSE 'orchestrator/worker' END AS next_action
                 FROM applications a
                WHERE a.status NOT IN ('submitted','abandoned')
                ORDER BY a.updated_at ASC LIMIT 30;"""
        )
        active = [{"application_id":r[0],"company":r[1],"job_title":r[2],"current_step":r[3],
                   "age_seconds":int(r[4] or 0),"processing_run_id":r[5],
                   "processing_lease_expires_at":str(r[6]) if r[6] else None,
                   "last_error":r[7],"next_action":r[8]} for r in cur.fetchall()]
        cur.execute("""SELECT id::text,hostname,pid,status,started_at,heartbeat_at,stopped_at
                         FROM runtime_instances ORDER BY heartbeat_at DESC LIMIT 1;""")
        rr=cur.fetchone()
        database_runtime = ({"id":rr[0],"hostname":rr[1],"pid":rr[2],"status":rr[3],
                             "started_at":str(rr[4]),"heartbeat_at":str(rr[5]),
                             "stopped_at":str(rr[6]) if rr[6] else None} if rr else None)
        cur.execute("SELECT status,count(*) FROM workflow_runs GROUP BY status ORDER BY status;")
        workflow_runs = {str(k):int(v) for k,v in cur.fetchall()}
        cur.execute("SELECT status,count(*) FROM control_commands GROUP BY status ORDER BY status;")
        control_commands = {str(k):int(v) for k,v in cur.fetchall()}
        cur.execute(
            """SELECT task_key,consecutive_failures,last_success_at,last_failure_at,last_error
                 FROM periodic_task_health
                WHERE consecutive_failures > 0
                ORDER BY consecutive_failures DESC,last_failure_at DESC;"""
        )
        periodic_failures = [
            {"task": row[0], "consecutive_failures": int(row[1] or 0),
             "last_success_at": str(row[2]) if row[2] else None,
             "last_failure_at": str(row[3]) if row[3] else None,
             "last_error": str(row[4] or "")[:300]}
            for row in cur.fetchall()
        ]
        if periodic_failures:
            runtime["ready"] = False
            runtime["periodic_failures"] = [entry["task"] for entry in periodic_failures]
    print(json.dumps({
        "runtime": runtime, "database_runtime": database_runtime,
        "workflow_runs": workflow_runs, "control_commands": control_commands,
        "periodic_task_failures": periodic_failures,
        "applications_by_status": applications, "applications_by_step": steps, "applications_by_source": sources,
        "browser_tasks": browser_tasks, "attempts": attempts, "pending_human_reviews": pending_reviews,
        "needs_reconciliation": reconciliation,
        "autofill_needs_reconciliation": autofill_reconciliation,
        "privileged_needs_reconciliation": privileged_reconciliation,
        "active_work": active,
        "daily_control_surface": "Telegram /start",
        "submit": "telegram_human_approval_then_privileged_executor",
    }, indent=2))
    return 0


def discover_command(command: str, *, apply: bool = False, keywords: str = "",
                     location: str = "", max_results: int = 3, timeout: int = 300,
                     date_posted: str | None = None, experience_levels: list[str] | None = None,
                     employment_types: list[str] | None = None, work_modes: list[str] | None = None,
                     companies: list[str] | None = None, sort_by: str = "recent") -> int:
    """Run/status the autonomous planner or queue one explicit LinkedIn search."""
    load_repo_env()
    if command in {"run", "status"}:
        argv = [sys.executable, "-m", "services.discovery.autonomous_discovery_v1", command]
        if command == "run":
            argv.append("--apply" if apply else "--dry-run")
        return subprocess.call(argv, cwd=ROOT)
    argv = [
        sys.executable, str(ROOT / "services" / "discovery" / "linkedin_intake_v1.py"),
        "queue-discovery", "--keywords", keywords, "--location", location,
        "--max-results", str(max_results), "--timeout", str(timeout), "--sort-by", sort_by,
    ]
    if date_posted:
        argv.extend(("--date-posted", date_posted))
    for flag, values in (
        ("--experience-level", experience_levels or []),
        ("--employment-type", employment_types or []),
        ("--work-mode-filter", work_modes or []),
        ("--company", companies or []),
    ):
        for value in values:
            argv.extend((flag, value))
    return subprocess.call(argv, cwd=ROOT)


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
    argv = [sys.executable, "-m", "services.auth.gmail_verification_v1",
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
    argv = [sys.executable, "-m", "services.auth.gmail_verification_watcher_v1",
            "--interval-seconds", str(interval_seconds), "--max-results", str(max_results)]
    if once:
        argv.append("--once")
    if wake_listen:
        argv.extend(("--wake-listen", "--wake-host", wake_host, "--wake-port", str(wake_port)))
    return subprocess.call(argv, cwd=ROOT)

def runtime_supervisor_command(command: str) -> int:
    load_repo_env()
    return subprocess.call([sys.executable, str(ROOT / "scripts" / "jobos_runtime_supervisor.py"), command], cwd=ROOT)


def _command_kind(args) -> str:
    nested = None
    for name in ("autofill_command","discover_command","saved_command","review_command","telegram_command",
                 "action_command","vault_command","gmail_command"):
        value = getattr(args,name,None)
        if value:
            nested = str(value); break
    return f"{args.command}:{nested}" if nested else str(args.command)


def _begin_control_command(args) -> tuple[str | None, str | None]:
    if args.command == "status":
        return None,None
    try:
        import psycopg
        from psycopg.types.json import Jsonb
        load_repo_env()
        workflow_id=str(uuid.uuid4())
        command_id=str(uuid.uuid4())
        arguments={k:v for k,v in vars(args).items() if k != "command"}
        kind=_command_kind(args)
        with psycopg.connect(database_dsn(),autocommit=False) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO workflow_runs(id,workflow_kind,subject_type,subject_id,status,requested_by,started_at)
                   VALUES (%s::uuid,'operator_command','control_command',%s,'running',%s,now());""",
                (workflow_id,command_id,os.getenv("USER") or "jobos-operator"),
            )
            cur.execute(
                """INSERT INTO control_commands(id,command_kind,arguments_json,requested_by,workflow_run_id,status)
                   VALUES (%s::uuid,%s,%s,%s,%s::uuid,'running');""",
                (command_id,kind,Jsonb(arguments),os.getenv("USER") or "jobos-operator",workflow_id),
            )
            conn.commit()
        return workflow_id,command_id
    except Exception:
        # start/doctor must remain usable while PostgreSQL itself is being repaired.
        return None,None


def _finish_control_command(ids: tuple[str | None,str | None], *, rc: int, error: str | None = None) -> None:
    workflow_id,command_id=ids
    if not workflow_id or not command_id:
        return
    try:
        import psycopg
        status_value="completed" if rc == 0 and not error else "failed"
        with psycopg.connect(database_dsn(),autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE control_commands SET status=%s,exit_code=%s,finished_at=now() WHERE id=%s::uuid;""",
                (status_value,rc,command_id),
            )
            cur.execute(
                """UPDATE workflow_runs SET status=%s,error_message=%s,finished_at=now() WHERE id=%s::uuid;""",
                (status_value,error[:2000] if error else None,workflow_id),
            )
    except Exception:
        pass


def _dispatch(args) -> int:
    if args.command in {"start", "stop"}:
        return runtime_supervisor_command(args.command)
    if args.command == "doctor":
        return doctor(check_browser=args.check_browser, strict=args.strict, require_autofill=args.require_autofill, profile=args.profile)
    if args.command == "verify-release":
        return verify_release(profile=args.profile)
    if args.command == "build-release":
        return subprocess.call([sys.executable, str(ROOT / "scripts" / "build_release.py"),
                                "--output", str(Path(args.output).expanduser())], cwd=ROOT)
    if args.command == "status":
        return status()
    if args.command == "discover":
        return discover_command(
            args.discover_command, apply=getattr(args, "apply", False),
            keywords=getattr(args, "keywords", ""), location=getattr(args, "location", ""),
            max_results=getattr(args, "max_results", 3), timeout=getattr(args, "timeout", 300),
            date_posted=getattr(args, "date_posted", None),
            experience_levels=getattr(args, "experience_level", []),
            employment_types=getattr(args, "employment_type", []),
            work_modes=getattr(args, "work_mode_filter", []),
            companies=getattr(args, "company", []),
            sort_by=getattr(args, "sort_by", "recent"),
        )
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


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS operator commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start", help="Start the configured JobOS workers behind one local supervisor.")
    commands.add_parser("stop", help="Stop the local JobOS worker supervisor cleanly.")
    doctor_parser = commands.add_parser("doctor", help="Readiness and safety checks; may remove a proven global OpenClaw package only after explicit y confirmation.")
    doctor_parser.add_argument("--check-browser", action="store_true", help="Also probe gateway and CDP health; never opens a page.")
    doctor_parser.add_argument("--strict", action="store_true", help="Exit non-zero unless the selected readiness profile is ready.")
    doctor_parser.add_argument("--profile", choices=("core","documents","browser","production"), default="production", help="Readiness lane to enforce; production includes optional operational channels.")
    doctor_parser.add_argument("--require-autofill", action="store_true", help="Exit non-zero unless deterministic form-fill readiness is ready.")
    verify_parser = commands.add_parser("verify-release", help="Run the fail-closed v0.1.0 release gate, including DB and controlled CDP acceptance.")
    verify_parser.add_argument("--profile", choices=(RELEASE_PROFILE,), default=RELEASE_PROFILE)
    build_parser = commands.add_parser("build-release", help="Build a deterministic tracked-source ZIP with an internal SHA-256 manifest.")
    build_parser.add_argument("--output", required=True, help="Destination .zip path (prefer outside the repository).")
    autofill_parser = commands.add_parser("autofill", help="Prepare a deterministic, approval-bound form session.")
    autofill_subcommands = autofill_parser.add_subparsers(dest="autofill_command", required=True)
    prepare_parser = autofill_subcommands.add_parser("prepare", help="Inspect the pinned application tab and optionally create its exact approval.")
    prepare_parser.add_argument("--application-id", required=True)
    prepare_parser.add_argument("--create", action="store_true", help="After showing the plan, prompt to create a one-time approval.")
    prepare_parser.add_argument("--yes", action="store_true", help="Create without an interactive confirmation; use only after reviewing the printed plan.")
    commands.add_parser("status", help="Read-only product status: applications, browser tasks, attempts, and human reviews.")

    discover_parser = commands.add_parser("discover", help="Profile-driven autonomous and explicit job discovery.")
    discover_sub = discover_parser.add_subparsers(dest="discover_command", required=True)
    discover_run = discover_sub.add_parser("run", help="Plan one discovery cycle; dry-run unless --apply is given.")
    discover_run.add_argument("--apply", action="store_true", help="Queue due autonomous discovery work.")
    discover_sub.add_parser("status", help="Show approved terms, queued planner work and ATS source count.")
    discover_linkedin = discover_sub.add_parser("linkedin", help="Queue one explicit bounded LinkedIn search.")
    discover_linkedin.add_argument("--keywords", required=True)
    discover_linkedin.add_argument("--location", default="")
    discover_linkedin.add_argument("--max-results", type=int, default=3)
    discover_linkedin.add_argument("--date-posted", choices=("24h", "week", "month"))
    discover_linkedin.add_argument("--experience-level", action="append", default=[])
    discover_linkedin.add_argument("--employment-type", action="append", default=[])
    discover_linkedin.add_argument("--work-mode-filter", action="append", default=[])
    discover_linkedin.add_argument("--company", action="append", default=[])
    discover_linkedin.add_argument("--sort-by", choices=("recent", "relevant"), default="recent")
    discover_linkedin.add_argument("--timeout", type=int, default=300)

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
    command_ids = _begin_control_command(args)
    try:
        rc = int(_dispatch(args))
    except Exception as exc:
        _finish_control_command(command_ids, rc=1, error=str(exc))
        raise
    _finish_control_command(command_ids, rc=rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
