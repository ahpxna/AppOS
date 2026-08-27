#!/usr/bin/env python3
"""Unified Human Review Hub.

This is a materialized UI/orchestration boundary, not a second approval engine.
Document decisions are exact-content-bound; capability decisions delegate to
approval_service_v1; post-autofill review never submits an application.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.common.config import database_dsn, load_repo_env

load_repo_env()
DSN = database_dsn()
SERVICE_VERSION = "human_review_hub_v1_2026_08_24"


class ReviewError(RuntimeError):
    pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _focus_or_open_exact_job_page(job_url: str) -> str:
    """Put the exact stored job page in the dedicated JobOS browser.

    This is a user-requested UI handoff, not a privileged Apply click.  It is
    deliberately strict: an existing exact tab is focused; otherwise exactly
    one new tab is opened.  Ambiguous tabs never become an implicit browser
    binding for a later capability.
    """
    from services.application_actions.privileged_action_v1 import _transport
    from services.autofill.autofill_executor_v1 import TransportError
    from services.common.autofill_identity import canonical_page_url

    try:
        expected = canonical_page_url(job_url)
    except ValueError as exc:
        raise ReviewError("This application has no valid stored job URL to open.") from exc
    transport = _transport()
    matches: list[str] = []
    for tab in transport.tabs():
        target_id = transport._stable_id(tab)
        raw_url = str(tab.get("url") or "")
        if not target_id:
            continue
        try:
            if canonical_page_url(raw_url) == expected:
                matches.append(target_id)
        except ValueError:
            continue
    if len(matches) > 1:
        raise ReviewError("More than one JobOS browser tab matches this job. Close duplicates or focus the intended tab, then retry.")
    try:
        target = transport.focus(matches[0]) if matches else transport.open(job_url)
    except TransportError as exc:
        raise ReviewError(f"JobOS could not open/focus the dedicated browser page: {exc}") from exc
    try:
        if canonical_page_url(target.url) != expected:
            raise ReviewError("The opened page redirected away from the exact stored job URL; JobOS will not bind it automatically.")
    except ValueError as exc:
        raise ReviewError("The opened browser tab has no valid HTTP(S) job URL.") from exc
    return str(target.target_id)


def focus_bound_application_page(cur, application_id: str, *, browser_task_id: str | None = None) -> str:
    """Focus the exact dedicated-browser target durably bound to one application.

    Browser identity is resolved through the same canonical authority helper used
    by automatic preparation/execution. A long-lived target id is insufficient:
    its live URL must still equal one durable URL for this application.
    """
    from services.application_actions.privileged_action_v1 import _transport
    from services.common.application_browser_binding_v1 import (
        ApplicationBrowserBindingError, resolve_application_bound_target,
    )
    transport = _transport()
    try:
        bound = resolve_application_bound_target(
            cur, transport, application_id=application_id,
            browser_task_id=browser_task_id, allow_focused_rebind=False,
        )
        target = transport.focus(bound.target_id)
    except ApplicationBrowserBindingError as exc:
        raise ReviewError(str(exc)) from exc
    except Exception as exc:
        raise ReviewError(f"The exact JobOS browser page is unavailable: {exc}") from exc
    if str(target.target_id) != str(bound.target_id):
        raise ReviewError("JobOS browser focus returned a different target than the application-bound target.")
    return str(bound.target_id)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_bundle(cur, application_id: str, *, kind: str = "application") -> str:
    cur.execute("SELECT company, job_title FROM applications WHERE id = %s;", (application_id,))
    row = cur.fetchone()
    if not row:
        raise ReviewError(f"Application not found: {application_id}")
    title = f"{row[0] or 'Unknown company'} — {row[1] or 'Unknown role'}"
    cur.execute(
        """INSERT INTO review_bundles(application_id, bundle_kind, title, status)
           VALUES (%s, %s, %s, 'pending')
           ON CONFLICT (application_id, bundle_kind) DO UPDATE
             SET title = EXCLUDED.title, updated_at = now()
           RETURNING id::text;""",
        (application_id, kind, title),
    )
    return str(cur.fetchone()[0])


def bind_canonical_review_pdf(cur, review_item_id: str, document_id: str, doc_type: str) -> str | None:
    """Bind one verified PDF for the exact generated-document slot."""
    cur.execute(
        """SELECT id::text, file_path, filename, sha256
             FROM generated_document_artifacts
            WHERE generated_document_id = %s
              AND artifact_type = %s
              AND lower(filename) LIKE '%%.pdf'
            ORDER BY created_at DESC;""",
        (document_id, doc_type),
    )
    kind = "resume_pdf" if doc_type == "resume" else "cover_letter_pdf"
    for artifact_id, file_path, filename, stored_sha in cur.fetchall():
        path = Path(file_path).expanduser()
        if not path.is_file():
            continue
        actual_sha = _sha256_file(path)
        if stored_sha != actual_sha:
            continue
        cur.execute(
            """INSERT INTO human_review_artifacts(
                   review_item_id, generated_document_artifact_id, artifact_kind,
                   file_path, filename, mime_type, sha256)
               VALUES (%s, %s, %s, %s, %s, 'application/pdf', %s)
               ON CONFLICT (review_item_id, artifact_kind, sha256) DO NOTHING;""",
            (review_item_id, artifact_id, kind, str(path.resolve()), filename, actual_sha),
        )
        cur.execute(
            """SELECT id::text FROM human_review_artifacts
                 WHERE review_item_id = %s AND generated_document_artifact_id = %s
                   AND artifact_kind = %s AND sha256 = %s;""",
            (review_item_id, artifact_id, kind, actual_sha),
        )
        bound = cur.fetchone()
        if bound:
            cur.execute(
                """UPDATE human_review_items
                      SET reviewed_artifact_id = %s,
                          payload_json = payload_json - 'artifact_render_error',
                          updated_at = now()
                    WHERE id = %s;""",
                (bound[0], review_item_id),
            )
            return str(bound[0])
    return None


def _ensure_review_pdf(cur, item_id: str, document_id: str, doc_type: str, qa_status: str) -> str | None:
    """Bind/render the canonical review PDF without changing review identity."""
    reviewed_artifact_id = bind_canonical_review_pdf(cur, item_id, document_id, doc_type)
    auto_render = os.getenv("JOBOS_REVIEW_AUTO_RENDER_PDFS", "true").strip().casefold() in {
        "1", "true", "yes", "on"
    }
    if reviewed_artifact_id or qa_status != "pass" or not auto_render:
        return reviewed_artifact_id
    try:
        from services.review.render_review_artifacts_v1 import render_document_pdf

        render_document_pdf(cur, document_id)
        return bind_canonical_review_pdf(cur, item_id, document_id, doc_type)
    except Exception as exc:
        cur.execute(
            """UPDATE human_review_items
                  SET payload_json = payload_json || %s, updated_at = now()
                WHERE id = %s;""",
            (Jsonb({"artifact_render_error": str(exc)[:500]}), item_id),
        )
        return None


def _set_document_review_state(cur, item_id: str, *, qa_status: str,
                               reviewed_artifact_id: str | None, summary: str,
                               target_status: str) -> None:
    """Materialize the actionable status for one stable review item."""
    status = target_status
    summary_text = summary
    if qa_status == "pass" and not reviewed_artifact_id:
        status = "needs_revision"
        summary_text = summary + " Canonical PDF artifact is required before approval."
    cur.execute(
        """UPDATE human_review_items
              SET status = %s, summary_text = %s, updated_at = now()
            WHERE id = %s;""",
        (status, summary_text, item_id),
    )


def ensure_document_review(cur, document_id: str) -> str | None:
    cur.execute(
        """SELECT gd.application_id::text, gd.doc_type, gd.version, gd.content,
                  gd.qa_status, gd.approved, a.company, a.job_title
             FROM generated_documents gd JOIN applications a ON a.id = gd.application_id
            WHERE gd.id = %s;""",
        (document_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ReviewError(f"Generated document not found: {document_id}")
    app_id, doc_type, version, content, qa_status, approved, company, role = row
    if doc_type not in {"resume", "cover_letter"} or qa_status not in {"pass", "fail", "revise"} or approved:
        return None

    # Serialize review creation per application. Without this lock, two concurrent
    # sync workers can both observe an empty slot and race into the unique index.
    cur.execute("SELECT id::text FROM applications WHERE id = %s FOR UPDATE;", (app_id,))
    if not cur.fetchone():
        raise ReviewError(f"Application not found: {app_id}")

    bundle_id = ensure_bundle(cur, app_id)
    source_sha = _sha256_text(content or "")
    label = "Resume" if doc_type == "resume" else "Cover letter"
    target_status = "pending" if qa_status == "pass" else "needs_revision"
    title = f"Review {label} v{version}" if qa_status == "pass" else f"Fix {label} v{version}: QA {qa_status}"
    summary = (
        f"{company} — {role}. QA passed; human approval is required before use."
        if qa_status == "pass"
        else f"{company} — {role}. Truth/quality gate returned {qa_status}; revise/regenerate before approval."
    )

    cur.execute(
        """SELECT id::text, status, source_sha256, payload_json,
                  generated_document_id::text
             FROM human_review_items
            WHERE application_id = %s AND item_type = 'document_review'
              AND payload_json->>'doc_type' = %s
              AND status IN ('pending','needs_revision')
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE;""",
        (app_id, doc_type),
    )
    existing = cur.fetchone()
    if existing:
        existing_qa = str((existing[3] or {}).get("qa_status") or "")
        same_review_identity = (
            existing[4] == str(document_id)
            and existing[2] == source_sha
            and existing_qa == qa_status
        )
        if same_review_identity:
            payload = existing[3] or {}
            if bool(payload.get("human_revision_required")):
                # A human Revise decision is an explicit gate. Do not turn the
                # unchanged document back into a pending approval merely because
                # its PDF is now renderable. Only a new content/version/QA
                # identity may supersede this review item.
                return str(existing[0])
            # A missing PDF is a materialized state problem, not a new review
            # identity. Re-sync the same item instead of expiring/recreating it.
            reviewed_artifact_id = _ensure_review_pdf(
                cur, existing[0], document_id, doc_type, qa_status
            )
            _set_document_review_state(
                cur, existing[0], qa_status=qa_status,
                reviewed_artifact_id=reviewed_artifact_id,
                summary=summary, target_status=target_status,
            )
            return str(existing[0])

        cur.execute(
            """UPDATE human_review_items
                  SET status = 'expired', decision_note = %s,
                      decided_at = now(), updated_at = now()
                WHERE id = %s;""",
            ("Document content, version, or QA state changed; a fresh review item was issued.", existing[0]),
        )

    cur.execute(
        """SELECT 1 FROM human_review_items
             WHERE generated_document_id = %s AND item_type = 'document_review'
               AND source_sha256 = %s
               AND coalesce(payload_json->>'qa_status', '') = %s
               AND status IN ('approved','rejected','resolved')
             LIMIT 1;""",
        (document_id, source_sha, qa_status),
    )
    if cur.fetchone():
        return None

    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, status, generated_document_id,
               title, summary_text, source_sha256, priority, payload_json)
           VALUES (%s, %s, 'document_review', %s, %s, %s, %s, %s, 'high', %s)
           RETURNING id::text;""",
        (bundle_id, app_id, target_status, document_id, title, summary, source_sha,
         Jsonb({"doc_type": doc_type, "version": version, "qa_status": qa_status})),
    )
    item_id = str(cur.fetchone()[0])
    reviewed_artifact_id = _ensure_review_pdf(cur, item_id, document_id, doc_type, qa_status)
    _set_document_review_state(
        cur, item_id, qa_status=qa_status, reviewed_artifact_id=reviewed_artifact_id,
        summary=summary, target_status=target_status,
    )
    return item_id

def ensure_approval_review(cur, approval_request_id: str) -> str | None:
    cur.execute(
        """SELECT ar.application_id::text, ar.type, ar.status, ar.summary_text,
                  ar.token_expires_at, a.company, a.job_title, ar.payload_json
             FROM approval_requests ar LEFT JOIN applications a ON a.id = ar.application_id
            WHERE ar.id = %s AND ar.status = 'pending'
              AND ar.token_expires_at > now();""",
        (approval_request_id,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    app_id, approval_type, _status, summary, expires, company, role, approval_payload = row
    bundle_id = ensure_bundle(cur, app_id)
    review_payload = {
        "approval_type": approval_type,
        "expires_at": expires.isoformat() if expires else None,
        "delegated_to_autofill": bool((approval_payload or {}).get("delegated_to_autofill")),
        "parent_approval_request_id": (approval_payload or {}).get("parent_approval_request_id"),
    }
    source_sha = _sha256_text(json.dumps({
        "approval_request_id": approval_request_id,
        "approval_type": approval_type,
        "payload": approval_payload or {},
    }, sort_keys=True, separators=(",", ":"), default=str))
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, approval_request_id,
               title, summary_text, priority, payload_json, source_sha256)
           VALUES (%s, %s, 'approval_request', %s, %s, %s, 'urgent', %s, %s)
           ON CONFLICT (approval_request_id)
             WHERE approval_request_id IS NOT NULL AND item_type = 'approval_request' AND status = 'pending'
           DO UPDATE SET title = EXCLUDED.title, summary_text = EXCLUDED.summary_text,
                         payload_json = EXCLUDED.payload_json, source_sha256 = EXCLUDED.source_sha256,
                         updated_at = now()
           RETURNING id::text;""",
        (bundle_id, app_id, approval_request_id, f"Approval required: {approval_type}",
         summary or f"{company or ''} — {role or ''}", Jsonb(review_payload), source_sha),
    )
    return str(cur.fetchone()[0])


def ensure_autofill_review(cur, browser_task_id: str, *, screenshot_path: str | None = None,
                           result: dict[str, Any] | None = None) -> str | None:
    cur.execute(
        """SELECT bt.application_id::text, bt.status, bt.execution_state, bt.result_json,
                  a.company, a.job_title
             FROM browser_tasks bt JOIN applications a ON a.id = bt.application_id
            WHERE bt.id = %s AND bt.task_type = 'fill_application_form';""",
        (browser_task_id,),
    )
    row = cur.fetchone()
    if not row or row[1] != "completed":
        return None
    app_id, _status, execution_state, stored_result, company, role = row
    result = result or stored_result or {}
    verified = list(result.get("verified_refs") or [])
    failed = list(result.get("failed_refs") or [])
    paused = list(result.get("paused") or [])
    source_sha = _sha256_text(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str))
    bundle_id = ensure_bundle(cur, app_id, kind="autofill")
    review_status = "pending" if execution_state == "completed" else "needs_revision"
    cur.execute(
        """SELECT status, source_sha256 FROM human_review_items
             WHERE browser_task_id = %s AND item_type = 'autofill_review'
             LIMIT 1;""",
        (browser_task_id,),
    )
    existing = cur.fetchone()
    if existing and existing[0] in {'approved', 'rejected', 'resolved', 'expired'}:
        return None
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, status, browser_task_id,
               title, summary_text, source_sha256, priority, payload_json)
           VALUES (%s, %s, 'autofill_review', %s, %s, 'Review autofilled form', %s, %s, 'urgent', %s)
           ON CONFLICT (browser_task_id)
             WHERE browser_task_id IS NOT NULL AND item_type = 'autofill_review'
           DO UPDATE SET summary_text = EXCLUDED.summary_text,
                         source_sha256 = EXCLUDED.source_sha256,
                         payload_json = EXCLUDED.payload_json, updated_at = now()
           RETURNING id::text;""",
        (bundle_id, app_id, review_status, browser_task_id,
         f"{company} — {role}. Verified fields: {len(verified)}; failed: {len(failed)}; paused: {len(paused)}.",
         source_sha, Jsonb({"execution_state": execution_state, "verified_refs": verified,
                            "failed_refs": failed, "paused": paused, "submit": "human_only"})),
    )
    item_id = str(cur.fetchone()[0])
    if screenshot_path:
        path = Path(screenshot_path).expanduser()
        if path.is_file():
            digest = _sha256_file(path)
            cur.execute(
                """INSERT INTO human_review_artifacts(
                       review_item_id, artifact_kind, file_path, filename, mime_type, sha256)
                   VALUES (%s, 'autofill_screenshot', %s, %s, 'image/png', %s)
                   ON CONFLICT (review_item_id, artifact_kind, sha256) DO NOTHING;""",
                (item_id, str(path.resolve()), path.name, digest),
            )
            cur.execute(
                """UPDATE human_review_items
                      SET payload_json = payload_json || %s, updated_at = now()
                    WHERE id = %s;""",
                (Jsonb({"screenshot_sha256": digest}), item_id),
            )
    return item_id


def ensure_application_ready_review(cur, application_id: str) -> str | None:
    cur.execute("SELECT company, job_title, current_step, status FROM applications WHERE id = %s;", (application_id,))
    row = cur.fetchone()
    if not row or row[2] != "application_ready" or row[3] in {"submitted", "abandoned"}:
        return None
    company, role, _step, _status = row
    cur.execute(
        """SELECT id::text FROM human_review_items
            WHERE application_id = %s AND item_type = 'application_ready' AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1;""",
        (application_id,),
    )
    existing = cur.fetchone()
    if existing:
        return str(existing[0])
    bundle_id = ensure_bundle(cur, application_id)
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, status, title,
               summary_text, priority, payload_json)
           VALUES (%s, %s, 'application_ready', 'pending',
                   'Application page needs a refresh', %s, 'urgent', %s)
           ON CONFLICT (application_id)
             WHERE item_type = 'application_ready' AND status = 'pending'
           DO UPDATE SET summary_text = EXCLUDED.summary_text,
                         payload_json = EXCLUDED.payload_json, updated_at = now()
           RETURNING id::text;""",
        (bundle_id, application_id,
         f"{company} — {role}. JobOS could not identify the next safe application action from the current browser page. Tap Retry to inspect the page again; this does not click Next, accept terms, or submit.",
         Jsonb({"submit": "separate_privileged_gate", "browser_action_authorized": False,
                "approve_semantics": "retry_read_only_page_inspection"})),
    )
    return str(cur.fetchone()[0])


def ensure_action_required_review(cur, *, application_id: str, action_kind: str,
                                  title: str, summary: str, payload: dict[str, Any] | None = None,
                                  priority: str = "high") -> str | None:
    """Materialize an immutable non-executable refocus/binding handoff.

    The source hash is the human-visible decision identity. If the underlying
    candidate/context changes, the old active item is expired and a new review
    row is created; Telegram buttons for the old row can never authorize the
    replacement payload. A prior explicit rejection suppresses that exact
    source, while approved/resolved/expired items may rematerialize when the
    authoritative workflow still needs the handoff.
    """
    action_kind = str(action_kind or "").strip()
    if not action_kind:
        raise ReviewError("action_required item is missing action_kind")
    body = dict(payload or {})
    body["action_kind"] = action_kind
    source_sha = _sha256_text(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str))

    # Review lock order is application -> item. It also serializes concurrent
    # materializers for the same application/action kind.
    cur.execute("SELECT 1 FROM applications WHERE id=%s FOR UPDATE;", (application_id,))
    if not cur.fetchone():
        raise ReviewError("application for action_required review no longer exists")
    cur.execute(
        """SELECT id::text, source_sha256 FROM human_review_items
             WHERE application_id=%s AND item_type='action_required'
               AND payload_json->>'action_kind'=%s
               AND status IN ('pending','needs_revision')
             ORDER BY created_at DESC LIMIT 1 FOR UPDATE;""",
        (application_id, action_kind),
    )
    active = cur.fetchone()
    if active and str(active[1] or "") == source_sha:
        return str(active[0])
    if active:
        cur.execute(
            """UPDATE human_review_items SET status='expired', updated_at=now(),
                      decision_note=COALESCE(decision_note,'Superseded by a new exact handoff source.')
                  WHERE id=%s AND status IN ('pending','needs_revision');""",
            (active[0],),
        )

    # A rejected email candidate is a durable human decision about that exact
    # mail.  Refocus/retry handoffs are different: rejecting one cannot make a
    # still-required authoritative application state disappear forever.
    if action_kind.startswith("email_verification_"):
        cur.execute(
            """SELECT 1 FROM human_review_items
                 WHERE application_id=%s AND item_type='action_required'
                   AND source_sha256=%s AND status='rejected' LIMIT 1;""",
            (application_id, source_sha),
        )
        if cur.fetchone():
            return None

    bundle_id = ensure_bundle(cur, application_id)
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, status, title, summary_text,
               source_sha256, priority, payload_json)
           VALUES (%s,%s,'action_required','pending',%s,%s,%s,%s,%s)
           RETURNING id::text;""",
        (bundle_id, application_id, title, summary, source_sha, priority, Jsonb(body)),
    )
    return str(cur.fetchone()[0])


def sync_action_required(cur) -> int:
    """Self-heal workflow handoffs that require the user to refocus a browser page."""
    count = 0
    cur.execute(
        """SELECT a.id::text, a.job_url, a.company, a.job_title,
                  a.approved_resume_id::text, a.approved_resume_artifact_id::text
             FROM applications a
            WHERE a.current_step='docs_verified' AND a.status NOT IN ('submitted','abandoned')
              AND a.approved_resume_id IS NOT NULL AND a.approved_resume_artifact_id IS NOT NULL
              AND NOT EXISTS (
                    SELECT 1 FROM approval_requests ar
                     WHERE ar.application_id=a.id AND ar.type='privileged_begin_application'
                       AND ar.status IN ('pending','approved','executing') AND ar.token_expires_at>now())
            ORDER BY a.updated_at DESC;"""
    )
    for app_id, job_url, company, role, resume_id, artifact_id in cur.fetchall():
        if ensure_action_required_review(
            cur, application_id=app_id, action_kind="open_apply_binding_required",
            title="Open/focus the job page to prepare Apply",
            summary=(f"{company} — {role}. Resume approval is complete. Open or focus the exact stored job page, "
                     "then approve this handoff to create a fresh OPEN APPLY approval. No browser click occurs here."),
            payload={"job_url": str(job_url or ""), "resume_id": resume_id, "resume_artifact_id": artifact_id},
            priority="high",
        ):
            count += 1
    return count


def sync_workflow_followup_required(cur) -> int:
    """Surface a durable retry handoff when post-commit gate packaging was lost."""
    count = 0
    cur.execute(
        """SELECT a.id::text, a.current_step, a.company, a.job_title,
                  s.current_url, s.page_fingerprint, s.detail_json
             FROM applications a
             LEFT JOIN application_auth_sessions s ON s.application_id=a.id
            WHERE a.current_step IN ('needs_account_auth','needs_email_verification','needs_mfa','needs_human_checkpoint','application_form_ready')
              AND a.status NOT IN ('submitted','abandoned')
              AND NOT EXISTS (
                    SELECT 1 FROM approval_requests ar
                     WHERE ar.application_id=a.id AND ar.status IN ('pending','approved','executing')
                       AND ar.token_expires_at>now())
              AND NOT EXISTS (
                    SELECT 1 FROM browser_tasks bt
                     WHERE bt.application_id=a.id AND bt.status IN ('queued','running')
                       AND bt.task_type='fill_application_form')
            ORDER BY a.updated_at DESC;"""
    )
    for app_id, state, company, role, url, fp, detail in cur.fetchall():
        target_id = str((detail or {}).get("target_id") or "")
        if ensure_action_required_review(
            cur, application_id=app_id, action_kind="workflow_followup_required",
            title="Refocus the current application page and retry the next gate",
            summary=(f"{company} — {role} is durably at {state}, but no live next-gate capability exists. "
                     "Refocus the exact employer page and approve this handoff. JobOS will fresh-snapshot it and "
                     "materialize the next auth/autofill gate without replaying the prior browser action."),
            payload={"expected_step": state, "target_id": target_id,
                     "expected_url": str(url or ""), "expected_page_fingerprint": str(fp or "")},
            priority="high",
        ):
            count += 1
    return count


def ensure_reconciliation_review(cur, browser_task_id: str) -> str | None:
    cur.execute(
        """SELECT bt.application_id::text, bt.error_message, a.company, a.job_title
             FROM browser_tasks bt JOIN applications a ON a.id = bt.application_id
            WHERE bt.id = %s AND bt.execution_state = 'needs_reconciliation';""",
        (browser_task_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    app_id, error, company, role = row
    bundle_id = ensure_bundle(cur, app_id, kind="reconciliation")
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, browser_task_id,
               title, summary_text, priority, payload_json)
           VALUES (%s, %s, 'reconciliation_required', %s,
                   'Autofill reconciliation required', %s, 'urgent', %s)
           ON CONFLICT (browser_task_id)
             WHERE browser_task_id IS NOT NULL AND item_type = 'reconciliation_required'
               AND status IN ('pending','needs_revision')
           DO UPDATE SET summary_text = EXCLUDED.summary_text,
                         payload_json = EXCLUDED.payload_json, updated_at = now()
           RETURNING id::text;""",
        (bundle_id, app_id, browser_task_id,
         f"{company} — {role}. Browser write state is uncertain; do not retry automatically.",
         Jsonb({"error": error or "unknown", "browser_task_id": browser_task_id})),
    )
    return str(cur.fetchone()[0])


def ensure_privileged_reconciliation_review(cur, execution_id: str) -> str | None:
    """Materialize uncertain privileged browser I/O as a first-class Human Review item."""
    cur.execute(
        """SELECT pae.application_id::text, pae.approval_request_id::text, pae.action_type,
                  pae.error_message, pae.expected_url, pae.observed_url,
                  pae.expected_page_fingerprint, pae.observed_page_fingerprint,
                  a.company, a.job_title
             FROM privileged_action_executions pae
             JOIN applications a ON a.id = pae.application_id
            WHERE pae.id = %s AND pae.status = 'needs_reconciliation';""",
        (execution_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    (app_id, approval_request_id, action_type, error, expected_url, observed_url,
     expected_fp, observed_fp, company, role) = row
    source_sha = _sha256_text(f"privileged|{execution_id}|{approval_request_id}|{action_type}")
    cur.execute(
        """SELECT id::text FROM human_review_items
             WHERE application_id = %s AND item_type = 'reconciliation_required'
               AND source_sha256 = %s AND status IN ('pending','needs_revision')
             ORDER BY created_at DESC LIMIT 1;""",
        (app_id, source_sha),
    )
    existing = cur.fetchone()
    if existing:
        return str(existing[0])
    bundle_id = ensure_bundle(cur, app_id, kind="reconciliation")
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, status, title, summary_text,
               source_sha256, priority, payload_json)
           VALUES (%s, %s, 'reconciliation_required', 'pending',
                   'Privileged action reconciliation required', %s, %s, 'urgent', %s)
           RETURNING id::text;""",
        (bundle_id, app_id,
         f"{company} — {role}. {action_type} may have had an external effect. Confirm occurred or not occurred; never replay this approval automatically.",
         source_sha, Jsonb({
             "privileged_execution_id": execution_id,
             "approval_request_id": approval_request_id,
             "action_type": action_type,
             "error": error or "unknown",
             "expected_url": expected_url,
             "observed_url": observed_url,
             "expected_page_fingerprint": expected_fp,
             "observed_page_fingerprint": observed_fp,
             "allowed_outcomes": ["occurred", "not_occurred"],
         })),
    )
    return str(cur.fetchone()[0])


def ensure_runtime_question_review(cur, *, application_id: str, browser_task_id: str,
                                   question: str, reason: str = "", profile_key: str | None = None) -> str | None:
    """Turn an unknown runtime ATS question into an answerable Human Review item."""
    from services.common.question_memory import normalize_question
    from services.common.immigration_semantics import classify_immigration_question, legal_question_pause_reason
    question = (question or "").strip()
    normalized = normalize_question(question)
    if not normalized:
        return None
    cur.execute("SELECT company, job_title FROM applications WHERE id = %s;", (application_id,))
    app = cur.fetchone()
    if not app:
        return None
    question_class = classify_immigration_question(question)
    if question_class is not None:
        source_sha = _sha256_text(f"runtime-sensitive|{application_id}|{browser_task_id}|{normalized}|{question_class.value}")
        cur.execute(
            """SELECT id::text,status FROM human_review_items
                 WHERE application_id=%s AND item_type='sensitive_question_required'
                   AND source_sha256=%s
                 ORDER BY created_at DESC LIMIT 1;""",
            (application_id, source_sha),
        )
        existing = cur.fetchone()
        if existing:
            # Historical paused_fields are scanned repeatedly. Once the exact
            # task/question source has a terminal human decision, never resurrect
            # it from that historical browser task. A later browser task gets a
            # different source SHA and may legitimately surface the question again.
            return str(existing[0]) if existing[1] in {'pending','needs_revision'} else None
        bundle_id = ensure_bundle(cur, application_id)
        # This card intentionally does not save generic question memory or
        # execute a browser fill.  It makes the exact legal wording visible in
        # the single inbox and directs the candidate to answer it manually on
        # the bound JobOS form.
        cur.execute(
            """INSERT INTO human_review_items(
                   review_bundle_id,application_id,item_type,status,browser_task_id,
                   title,summary_text,source_sha256,priority,payload_json)
               VALUES (%s,%s,'sensitive_question_required','pending',%s,%s,%s,%s,'urgent',%s)
               RETURNING id::text;""",
            (bundle_id, application_id, browser_task_id,
             f"Sensitive answer required: {question[:110]}",
             f"{app[0]} — {app[1]}. {legal_question_pause_reason(question)}",
             source_sha, Jsonb({
                 "question": question, "question_normalized": normalized,
                 "question_class": question_class.value, "browser_task_id": browser_task_id,
                 "source": "runtime_sensitive_autofill_pause",
                 "manual_only": True,
             })),
        )
        return str(cur.fetchone()[0])
    source_sha = _sha256_text(f"runtime|{application_id}|{browser_task_id}|{normalized}")
    cur.execute(
        """SELECT id::text,status FROM human_review_items
             WHERE application_id=%s AND item_type='question_required'
               AND source_sha256=%s
             ORDER BY created_at DESC LIMIT 1;""",
        (application_id, source_sha),
    )
    existing = cur.fetchone()
    if existing:
        return str(existing[0]) if existing[1] in {'pending','needs_revision'} else None
    bundle_id = ensure_bundle(cur, application_id)
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, status, title, summary_text,
               source_sha256, priority, payload_json)
           VALUES (%s, %s, 'question_required', 'pending', %s, %s, %s, 'high', %s)
           ON CONFLICT (application_id, source_sha256)
             WHERE source_sha256 IS NOT NULL AND item_type = 'question_required'
               AND status IN ('pending','needs_revision')
           DO UPDATE SET title = EXCLUDED.title, summary_text = EXCLUDED.summary_text,
                         payload_json = EXCLUDED.payload_json, updated_at = now()
           RETURNING id::text;""",
        (bundle_id, application_id, f"Answer required: {question[:110]}",
         f"{app[0]} — {app[1]}. {reason or 'This question appeared only at runtime and has no approved answer.'}",
         source_sha, Jsonb({
             "question": question,
             "question_normalized": normalized,
             "missing_information": reason,
             "browser_task_id": browser_task_id,
             "profile_key": profile_key,
             "source": "runtime_autofill_pause",
         })),
    )
    return str(cur.fetchone()[0])


def sync_runtime_questions(cur) -> int:
    cur.execute(
        """SELECT bt.id::text, bt.application_id::text, bt.result_json
             FROM browser_tasks bt
             JOIN applications a ON a.id=bt.application_id
            WHERE bt.task_type = 'fill_application_form' AND bt.status = 'completed'
              AND a.status NOT IN ('submitted','abandoned')
              AND jsonb_typeof(coalesce(bt.result_json, '{}'::jsonb)->'paused_fields') = 'array';"""
    )
    count = 0
    for task_id, app_id, result in cur.fetchall():
        for paused in (result or {}).get("paused_fields") or []:
            if not isinstance(paused, dict):
                continue
            question = str(paused.get("question") or "").strip()
            if not question:
                continue
            if ensure_runtime_question_review(
                cur, application_id=app_id, browser_task_id=task_id, question=question,
                reason=str(paused.get("reason") or "").strip(),
                profile_key=str(paused.get("profile_key") or "").strip() or None,
            ):
                count += 1
    return count


def ensure_question_review(cur, *, application_id: str, document_id: str,
                           question: str, missing_information: str = "") -> str | None:
    from services.common.question_memory import normalize_question
    from services.common.immigration_semantics import legal_question_pause_reason
    normalized = normalize_question(question)
    # Generic document-question memory may never turn legal/immigration text
    # into a one-tap Yes/No card.  Those answers require the separate exact,
    # candidate-confirmed workflow.
    if not normalized or legal_question_pause_reason(question):
        return None
    cur.execute("SELECT company, job_title FROM applications WHERE id = %s;", (application_id,))
    app = cur.fetchone()
    if not app:
        return None
    bundle_id = ensure_bundle(cur, application_id)
    source_sha = _sha256_text(f"{document_id}|{normalized}|{missing_information}")
    cur.execute(
        """SELECT 1 FROM human_review_items
             WHERE application_id = %s AND item_type = 'question_required'
               AND source_sha256 = %s AND status IN ('approved','rejected','resolved','expired')
             LIMIT 1;""",
        (application_id, source_sha),
    )
    if cur.fetchone():
        return None
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, status, generated_document_id,
               title, summary_text, source_sha256, priority, payload_json)
           VALUES (%s, %s, 'question_required', 'pending', %s, %s, %s, %s, 'high', %s)
           ON CONFLICT (application_id, source_sha256)
             WHERE source_sha256 IS NOT NULL AND item_type = 'question_required'
               AND status IN ('pending','needs_revision')
           DO UPDATE SET title = EXCLUDED.title, summary_text = EXCLUDED.summary_text,
                         payload_json = EXCLUDED.payload_json, updated_at = now()
           RETURNING id::text;""",
        (bundle_id, application_id, document_id, f"Answer required: {question[:110]}",
         f"{app[0]} — {app[1]}. {missing_information or 'The generator has no approved evidence for this answer.'}",
         source_sha, Jsonb({"question": question, "question_normalized": normalized,
                            "missing_information": missing_information, "document_id": document_id})),
    )
    return str(cur.fetchone()[0])


def sync_missing_questions(cur) -> int:
    cur.execute(
        """SELECT id::text, application_id::text, evidence_map
             FROM generated_documents
            WHERE doc_type = 'short_answers'
              AND (qa_status IS NULL OR qa_status IN ('pass','revise','fail'))
            ORDER BY created_at DESC;"""
    )
    from services.common.question_memory import normalize_question
    count = 0
    seen: set[tuple[str, str]] = set()
    for document_id, app_id, evidence_map in cur.fetchall():
        for claim in (evidence_map or {}).get("claims", []):
            if not isinstance(claim, dict) or claim.get("answerable") is not False:
                continue
            question = str(claim.get("claim") or claim.get("question") or "").strip()
            key = (app_id, normalize_question(question))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            if ensure_question_review(cur, application_id=app_id, document_id=document_id,
                                      question=question,
                                      missing_information=str(claim.get("missing_information") or "").strip()):
                count += 1
    return count


def answer_question(conn, item_id: str, *, answer: str, actor: str,
                    scope: str = "company", answer_kind: str = "text") -> dict[str, Any]:
    answer = (answer or "").strip()
    if not answer:
        raise ReviewError("Answer cannot be empty.")
    if scope not in {"global", "ats", "company"}:
        raise ReviewError("scope must be global, ats, or company")
    if answer_kind not in {"text", "option"}:
        raise ReviewError("answer_kind must be text or option")
    from services.common.question_memory import normalize_question
    from services.common.immigration_semantics import legal_question_pause_reason
    with conn.cursor() as cur:
        cur.execute(
            """SELECT h.status, h.application_id::text, h.payload_json,
                      a.company, coalesce(a.ats_type, '')
                 FROM human_review_items h JOIN applications a ON a.id = h.application_id
                WHERE h.id = %s AND h.item_type = 'question_required' FOR UPDATE;""",
            (item_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ReviewError("Question review item not found.")
        status, _app_id, payload, company, ats_type = row
        if status not in {"pending", "needs_revision"}:
            raise ReviewError(f"Question review item is already {status}.")
        question = str((payload or {}).get("question") or "").strip()
        if legal_question_pause_reason(question):
            raise ReviewError("This is a legal/immigration question. Use the dedicated immigration exact-answer workflow; generic question memory is forbidden.")
        normalized = normalize_question(question)
        company_normalized = normalize_question(company) if scope == "company" else None
        ats_value = ats_type if scope == "ats" else None
        cur.execute(
            """INSERT INTO application_question_memory(
                   scope, ats_type, company_normalized, question_normalized, answer_text,
                   answer_kind, confidence, user_confirmed_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, 1.0, now(), now())
               ON CONFLICT (scope, ats_type, company_normalized, question_normalized)
               DO UPDATE SET answer_text = EXCLUDED.answer_text,
                             answer_kind = EXCLUDED.answer_kind, confidence = 1.0,
                             user_confirmed_at = now(), updated_at = now();""",
            (scope, ats_value, company_normalized, normalized, answer, answer_kind),
        )
        cur.execute(
            """UPDATE human_review_items SET status = 'resolved', decided_by = %s,
                      decision_note = %s, decided_at = now(), updated_at = now()
                WHERE id = %s;""",
            (actor, f"Human supplied {scope}-scoped answer.", item_id),
        )
        browser_task_id = str((payload or {}).get("browser_task_id") or "")
        restored = _restore_form_ready_after_human_input(
            cur, application_id=_app_id, browser_task_id=browser_task_id, actor=actor,
            reason="Human answered every paused runtime question; create a fresh exact autofill plan.",
        )
    return {"ok": True, "review_item_id": item_id, "status": "resolved",
            "application_id": _app_id, "scope": scope, "question": normalized,
            "answer_kind": answer_kind, "autofill_reprepare_required": restored}


def _runtime_questions_resolved_for_task(cur, *, application_id: str, browser_task_id: str) -> bool:
    cur.execute(
        """SELECT count(*) FROM human_review_items
             WHERE application_id=%s AND browser_task_id=%s
               AND item_type IN ('question_required','sensitive_question_required')
               AND status IN ('pending','needs_revision');""",
        (application_id, browser_task_id),
    )
    if int((cur.fetchone() or (0,))[0] or 0) != 0:
        return False
    cur.execute(
        """SELECT count(*) FROM browser_tasks
             WHERE application_id=%s AND execution_state='needs_reconciliation';""",
        (application_id,),
    )
    if int((cur.fetchone() or (0,))[0] or 0) != 0:
        return False
    cur.execute(
        """SELECT count(*) FROM approval_requests
             WHERE application_id=%s AND type='autofill_form'
               AND status IN ('pending','approved','executing')
               AND (status='executing' OR token_expires_at>now());""",
        (application_id,),
    )
    if int((cur.fetchone() or (0,))[0] or 0) != 0:
        return False
    cur.execute(
        """SELECT count(*) FROM browser_tasks
             WHERE application_id=%s AND task_type='fill_application_form'
               AND status IN ('queued','running');""",
        (application_id,),
    )
    return int((cur.fetchone() or (0,))[0] or 0) == 0


def _restore_form_ready_after_human_input(cur, *, application_id: str, browser_task_id: str,
                                          actor: str, reason: str) -> bool:
    if not browser_task_id or not _runtime_questions_resolved_for_task(
        cur, application_id=application_id, browser_task_id=browser_task_id
    ):
        return False
    cur.execute("SELECT current_step FROM applications WHERE id=%s FOR UPDATE;", (application_id,))
    row = cur.fetchone()
    if not row or str(row[0] or "") not in {'awaiting_approval','form_filled'}:
        return False
    from services.application_actions.privileged_action_v1 import _transition_application_step
    _transition_application_step(
        cur, application_id=application_id, to_step='application_form_ready', actor=actor,
        reason=reason, detail={"browser_task_id": browser_task_id, "fresh_approval_required": True},
    )
    return True


def prepare_fresh_autofill_after_human_input(application_id: str) -> dict[str, Any]:
    """Create a fresh exact plan after human input; never reuse the old capability."""
    from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER
    command = [sys.executable, str(ROOT / 'scripts' / 'jobos.py'), 'autofill', 'prepare',
               '--application-id', application_id, '--create', '--yes']
    proc = DEFAULT_PROCESS_RUNNER.run(command, cwd=ROOT, timeout_s=180)
    detail = proc.output + (f"\n{proc.start_error}" if proc.start_error else "")
    return {"ok": proc.ok, "detail": detail[-1600:], "transient": proc.transient}


def _sensitive_question_completed(cur, *, application_id: str, browser_task_id: str, question: str) -> bool:
    """Verify the exact manual sensitive question is no longer unanswered."""
    from services.application_actions.privileged_action_v1 import _transport, _snapshot, detect_page_state
    from services.autofill.autofill_agent_v1 import parse_snapshot
    from services.autofill.form_inspector_v1 import inspect_nodes, inspect_question_groups
    from services.common.question_memory import normalize_question

    target_id = focus_bound_application_page(cur, application_id, browser_task_id=browser_task_id)
    transport = _transport()
    url, snap, nodes, _fp = _snapshot(transport, target_id)
    state, _detail = detect_page_state(url, snap, nodes)
    if state != 'application_form_ready':
        raise ReviewError(
            f"The bound page is now {state!r}, not the application form. Use the current workflow card instead of closing this question."
        )
    qn = normalize_question(question)
    fields = inspect_nodes(parse_snapshot(snap))
    groups = inspect_question_groups(parse_snapshot(snap))
    matched = False
    for field in fields:
        if normalize_question(field.label or '') == qn:
            matched = True
            value = str(field.value or '').strip()
            if not value:
                return False
    for group in groups:
        if normalize_question(group.label or '') == qn:
            matched = True
            if not any(option.selected is True for option in group.options):
                return False
    # If the exact question disappeared from the same bound form after manual
    # interaction, treat it as completed; the next fresh plan will re-snapshot
    # and can pause again if the ATS reintroduces it.
    return True if not matched else True


def submit_document_feedback(conn, item_id: str, *, feedback: str, actor: str) -> dict[str, Any]:
    """Queue a human-authored revision request for the exact reviewed draft."""
    feedback = (feedback or '').strip()
    if not feedback:
        raise ReviewError('Revision feedback cannot be empty.')
    if len(feedback) > 8000:
        raise ReviewError('Revision feedback is too long; keep it under 8000 characters.')
    with conn.cursor() as cur:
        cur.execute(
            """SELECT application_id::text FROM human_review_items WHERE id=%s;""",
            (item_id,),
        )
        seed = cur.fetchone()
        if not seed:
            raise ReviewError('Document review item not found.')
        cur.execute("SELECT id::text FROM applications WHERE id=%s FOR UPDATE;", (seed[0],))
        if not cur.fetchone():
            raise ReviewError('Application no longer exists.')
        cur.execute(
            """SELECT h.application_id::text,h.status,h.source_sha256,h.generated_document_id::text,
                      gd.doc_type,gd.content
                 FROM human_review_items h
                 JOIN generated_documents gd ON gd.id=h.generated_document_id
                WHERE h.id=%s AND h.item_type='document_review'
                FOR UPDATE OF h,gd;""",
            (item_id,),
        )
        row = cur.fetchone()
        if not row or row[1] not in {'pending','needs_revision'}:
            raise ReviewError('Document review item is no longer editable; use the newest review card.')
        app_id, _status, source_sha, document_id, doc_type, content = row
        if doc_type not in {'resume','cover_letter'}:
            raise ReviewError('Only resume and cover-letter review cards support agent revision feedback.')
        if _sha256_text(content or '') != str(source_sha or ''):
            raise ReviewError('Document content changed after this review card was created; use the newest card.')
        # Serialize feedback for the exact review item. Pending feedback may be
        # edited before a worker claims it, but a running request is immutable:
        # overwriting it would make the durable request say B while the worker
        # is already generating from A.
        cur.execute(
            """SELECT id::text,status FROM document_revision_requests
                 WHERE source_review_item_id=%s AND source_sha256=%s
                   AND status IN ('pending','running')
                 ORDER BY created_at DESC LIMIT 1 FOR UPDATE;""",
            (item_id, source_sha),
        )
        active_revision = cur.fetchone()
        if active_revision and str(active_revision[1]) == 'running':
            raise ReviewError('The document agent is already revising this exact draft. Wait for the fresh review card before sending more feedback.')
        if active_revision:
            request_id = str(active_revision[0])
            cur.execute(
                """UPDATE document_revision_requests
                      SET feedback_text=%s,requested_by=%s,error_message=NULL,updated_at=now()
                    WHERE id=%s AND status='pending';""",
                (feedback, actor, request_id),
            )
        else:
            cur.execute(
                """INSERT INTO document_revision_requests(
                       application_id,document_type,source_document_id,source_review_item_id,
                       source_sha256,feedback_text,status,requested_by,created_at,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,now(),now())
                   RETURNING id::text;""",
                (app_id, doc_type, document_id, item_id, source_sha, feedback, actor),
            )
            request_id = str(cur.fetchone()[0])
        cur.execute(
            """UPDATE human_review_items
                  SET status='needs_revision',
                      payload_json=payload_json || %s,
                      decision_note=%s,updated_at=now()
                WHERE id=%s;""",
            (Jsonb({"human_revision_required": True, "document_revision_request_id": request_id,
                    "human_feedback": feedback}),
             'Human sent direct feedback to the document agent; regeneration is queued.', item_id),
        )
    return {"ok": True, "review_item_id": item_id, "application_id": app_id,
            "document_type": doc_type, "revision_request_id": request_id}


def reconcile_materialized_review_state(cur) -> None:
    # Terminal applications must not keep ordinary daily-work cards/capabilities
    # alive. Reconciliation is intentionally excluded: an uncertain external
    # effect still needs a human verdict even after the application is stopped.
    cur.execute(
        """UPDATE approval_requests ar SET status='expired',executing_task_id=NULL
             FROM applications a
            WHERE ar.application_id=a.id AND a.status IN ('submitted','abandoned')
              AND ar.status IN ('pending','approved');"""
    )
    cur.execute(
        """UPDATE document_revision_requests drr
              SET status='cancelled',claimed_by=NULL,lease_expires_at=NULL,
                  error_message='Application became terminal before document revision execution.',
                  finished_at=now(),updated_at=now()
             FROM applications a
            WHERE drr.application_id=a.id AND a.status IN ('submitted','abandoned')
              AND drr.status='pending';"""
    )
    cur.execute(
        """UPDATE human_review_items h SET status='resolved',updated_at=now(),
                  decision_note=coalesce(h.decision_note,'Application became terminal; ordinary review closed.')
             FROM applications a
            WHERE h.application_id=a.id AND a.status IN ('submitted','abandoned')
              AND h.status IN ('pending','needs_revision')
              AND h.item_type IN ('document_review','autofill_review','question_required',
                                  'sensitive_question_required','application_ready','action_required');"""
    )

    # Expire stale parent autofill capabilities first, then restore the
    # recoverable application state and close delegated children. Otherwise a
    # timed-out approval leaves the application stranded at awaiting_approval.
    cur.execute(
        """UPDATE approval_requests
              SET status='expired', executing_task_id=NULL
            WHERE type='autofill_form' AND status IN ('pending','approved')
              AND token_expires_at <= now()
            RETURNING id::text, application_id::text, payload_json;"""
    )
    expired_autofill = [(str(row[0]), str(row[1]), dict(row[2] or {})) for row in cur.fetchall()]
    if expired_autofill:
        from services.approval.approval_service_v1 import _restore_autofill_ready_after_terminal_parent
        for parent_request_id, application_id, payload in expired_autofill:
            _restore_autofill_ready_after_terminal_parent(
                cur, application_id=application_id,
                plan_key=str(payload.get("autofill_plan_key") or ""),
                reason="Autofill approval expired before browser I/O.",
                parent_request_id=parent_request_id,
            )
    cur.execute(
        """UPDATE human_review_items h
              SET status = CASE WHEN ar.status IN ('approved','executing','consumed') THEN 'approved'
                                WHEN ar.status = 'denied' THEN 'rejected'
                                ELSE 'expired' END,
                  decided_at = coalesce(h.decided_at, now()), updated_at = now()
             FROM approval_requests ar
            WHERE h.approval_request_id = ar.id
              AND h.item_type = 'approval_request'
              AND h.status IN ('pending','needs_revision')
              AND (ar.status <> 'pending' OR ar.token_expires_at <= now());"""
    )
    cur.execute(
        """UPDATE human_review_items h SET status = 'approved', updated_at = now()
             FROM generated_documents gd
            WHERE h.generated_document_id = gd.id AND h.item_type = 'document_review'
              AND h.status IN ('pending','needs_revision') AND gd.approved = true;"""
    )
    cur.execute(
        """UPDATE human_review_items h SET status = 'resolved', updated_at = now()
             FROM browser_tasks bt
            WHERE h.browser_task_id = bt.id AND h.item_type = 'reconciliation_required'
              AND h.status IN ('pending','needs_revision')
              AND bt.execution_state <> 'needs_reconciliation';"""
    )
    cur.execute(
        """UPDATE human_review_items h SET status = 'resolved', updated_at = now()
             FROM privileged_action_executions pae
            WHERE h.item_type = 'reconciliation_required'
              AND h.status IN ('pending','needs_revision')
              AND h.payload_json->>'privileged_execution_id' = pae.id::text
              AND pae.status <> 'needs_reconciliation';"""
    )
    cur.execute(
        """UPDATE human_review_items h SET status = 'resolved', updated_at = now()
             FROM applications a
            WHERE h.application_id = a.id AND h.item_type = 'application_ready'
              AND h.status = 'pending' AND a.current_step IN ('submitted','abandoned');"""
    )
    cur.execute(
        """UPDATE human_review_items h SET status='resolved', updated_at=now()
             FROM applications a
            WHERE h.application_id=a.id AND h.item_type='action_required'
              AND h.status IN ('pending','needs_revision')
              AND (
                    (h.payload_json->>'action_kind'='open_apply_binding_required'
                     AND a.current_step <> 'docs_verified')
                 OR (h.payload_json->>'action_kind' LIKE 'email_verification_%'
                     AND a.current_step <> 'needs_email_verification')
                 OR (h.payload_json->>'action_kind'='workflow_followup_required'
                     AND a.current_step <> h.payload_json->>'expected_step')
              );"""
    )
    cur.execute(
        """UPDATE human_review_items h SET status='resolved', updated_at=now()
             FROM approval_requests ar
            WHERE h.application_id=ar.application_id AND h.item_type='action_required'
              AND h.status IN ('pending','needs_revision')
              AND ((h.payload_json->>'action_kind'='open_apply_binding_required'
                    AND ar.type='privileged_begin_application')
                OR (h.payload_json->>'action_kind' LIKE 'email_verification_%'
                    AND ar.type='privileged_use_email_verification'
                    AND COALESCE(h.payload_json->>'candidate_id','') <> ''
                    AND ar.payload_json->>'candidate_id' = h.payload_json->>'candidate_id')
                OR (h.payload_json->>'action_kind'='workflow_followup_required'
                    AND h.payload_json->>'expected_step'='needs_email_verification'
                    AND ar.type='privileged_use_email_verification'))
              AND ar.status IN ('pending','approved','executing');"""
    )


def sync_inbox(cur) -> dict[str, int]:
    reconcile_materialized_review_state(cur)
    counts = {"documents": 0, "questions": 0, "approvals": 0,
              "autofill": 0, "reconciliation": 0, "application_ready": 0,
              "action_required": 0}
    # Only the newest version in each application/document slot is actionable.
    # Historical versions remain auditable but must never create concurrent
    # Telegram/CLI decisions or violate the active-slot invariant.
    cur.execute(
        """SELECT DISTINCT ON (application_id, doc_type) id::text
             FROM generated_documents
            WHERE doc_type IN ('resume','cover_letter')
              AND qa_status IN ('pass','fail','revise')
              AND approved = false
            ORDER BY application_id, doc_type, version DESC, created_at DESC, id DESC;"""
    )
    for (doc_id,) in cur.fetchall():
        if ensure_document_review(cur, doc_id):
            counts["documents"] += 1
    counts["questions"] = sync_missing_questions(cur) + sync_runtime_questions(cur)
    counts["action_required"] += sync_action_required(cur)
    counts["action_required"] += sync_workflow_followup_required(cur)
    cur.execute("""SELECT id::text FROM approval_requests
                    WHERE status = 'pending' AND application_id IS NOT NULL
                      AND token_expires_at > now();""")
    for (request_id,) in cur.fetchall():
        if ensure_approval_review(cur, request_id):
            counts["approvals"] += 1
    cur.execute("""SELECT id::text, screenshot_url FROM browser_tasks
                    WHERE task_type = 'fill_application_form' AND status = 'completed';""")
    for task_id, screenshot_url in cur.fetchall():
        if ensure_autofill_review(cur, task_id, screenshot_path=screenshot_url):
            counts["autofill"] += 1
    cur.execute("SELECT id::text FROM browser_tasks WHERE execution_state = 'needs_reconciliation';")
    for (task_id,) in cur.fetchall():
        if ensure_reconciliation_review(cur, task_id):
            counts["reconciliation"] += 1
    cur.execute("SELECT id::text FROM privileged_action_executions WHERE status = 'needs_reconciliation';")
    for (execution_id,) in cur.fetchall():
        if ensure_privileged_reconciliation_review(cur, execution_id):
            counts["reconciliation"] += 1
    cur.execute("SELECT id::text FROM applications WHERE current_step = 'application_ready';")
    application_ready_ids = [str(row[0]) for row in cur.fetchall()]
    for app_id in application_ready_ids:
        # application_ready gate preparation is read-only browser inspection: it
        # snapshots the exact focused application page and materializes a bound
        # Consent/Next/Submit approval, but never clicks. Do it automatically so
        # daily UX does not require a meaningless "prepare next gate" tap.
        #
        # Once a fallback card is visible, do not hammer the browser on every
        # inbox refresh. The user's Retry action explicitly asks for a new check.
        cur.execute(
            """SELECT id::text FROM human_review_items
                 WHERE application_id=%s AND item_type='application_ready'
                   AND status IN ('pending','needs_revision')
                 ORDER BY created_at DESC LIMIT 1;""",
            (app_id,),
        )
        if cur.fetchone():
            counts["application_ready"] += 1
            continue

        # If an exact application-ready capability is already live, no fallback
        # review card is needed.
        cur.execute(
            """SELECT count(*) FROM approval_requests
                 WHERE application_id=%s
                   AND type IN ('privileged_accept_terms',
                                'privileged_advance_application_step',
                                'privileged_submit_application')
                   AND status IN ('pending','approved','executing')
                   AND (status='executing' OR token_expires_at > now());""",
            (app_id,),
        )
        if int((cur.fetchone() or (0,))[0] or 0) > 0:
            continue

        cur.execute("SAVEPOINT jobos_auto_prepare_application_ready")
        preparation_error = ""
        try:
            from services.application_actions.privileged_action_v1 import materialize_application_ready_gate
            prepared = materialize_application_ready_gate(cur, app_id)
        except Exception as exc:
            cur.execute("ROLLBACK TO SAVEPOINT jobos_auto_prepare_application_ready")
            prepared = []
            preparation_error = str(exc)[:500]
        finally:
            cur.execute("RELEASE SAVEPOINT jobos_auto_prepare_application_ready")

        if prepared:
            # create_privileged_request() materializes the exact Review Hub item.
            continue

        fallback_id = ensure_application_ready_review(cur, app_id)
        if fallback_id:
            counts["application_ready"] += 1
            if preparation_error:
                cur.execute(
                    """UPDATE human_review_items
                          SET payload_json = payload_json || %s, updated_at=now()
                        WHERE id=%s;""",
                    (Jsonb({"auto_prepare_error": preparation_error}), fallback_id),
                )
    return counts


def list_inbox(cur) -> list[dict[str, Any]]:
    cur.execute(
        """SELECT review_item_id::text, application_id::text, item_type, status,
                  priority, title, summary_text, company, job_title, created_at
             FROM v_human_review_inbox;"""
    )
    return [
        {"id": r[0], "application_id": r[1], "type": r[2], "status": r[3],
         "priority": r[4], "title": r[5], "summary": r[6], "company": r[7],
         "job_title": r[8], "created_at": r[9].isoformat() if r[9] else None}
        for r in cur.fetchall()
    ]


def safe_batch_review_items(cur, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return exact currently-actionable items eligible for one-tap safe approval.

    Classification is presentation policy only; every child review item still
    goes through decide_item(), preserving its exact hashes/capabilities.
    """
    from services.review.ux_policy_v1 import is_batch_safe_item
    cur.execute(
        """SELECT v.review_item_id::text, v.application_id::text, v.item_type,
                  v.payload_json, h.source_sha256, v.company, v.job_title,
                  h.status, h.reviewed_artifact_id::text
             FROM v_human_review_inbox v
             JOIN human_review_items h ON h.id = v.review_item_id
            ORDER BY CASE v.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                     v.created_at
            LIMIT %s;""",
        (max(1, min(int(limit), 100)),),
    )
    result = []
    for item_id, app_id, item_type, payload, source_sha, company, role, status, reviewed_artifact_id in cur.fetchall():
        payload = dict(payload or {})
        if not is_batch_safe_item(item_type=str(item_type), payload=payload):
            continue
        # A QA-pass document with no canonical reviewed PDF is deliberately
        # held at needs_revision.  Never advertise it inside a one-tap batch
        # that will fail later in decide_item().
        if str(item_type) == "document_review" and (str(status) != "pending" or not reviewed_artifact_id):
            continue
        result.append({
            "item_id": str(item_id), "application_id": str(app_id),
            "item_type": str(item_type), "payload": payload,
            "source_sha256": str(source_sha or ""),
            "company": company or "", "job_title": role or "",
        })
    return result


def snooze_review_item(conn, item_id: str, *, actor: str, hours: int = 6) -> dict[str, Any]:
    """Temporarily hide a review card without changing its underlying decision."""
    hours = max(1, min(int(hours), 72))
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE human_review_items
                  SET snoozed_until=now() + make_interval(hours => %s), updated_at=now(),
                      decision_note=coalesce(decision_note,'') || %s
                WHERE id=%s AND status IN ('pending','needs_revision')
                RETURNING application_id::text, snoozed_until;""",
            (hours, f"\nSnoozed by {actor} for {hours}h.", item_id),
        )
        row = cur.fetchone()
        if not row:
            raise ReviewError("Review item is no longer actionable.")
        # Later is an explicit user intent. Retire every live single-item
        # callback and any pending free-text capture so an old Telegram card
        # cannot approve an item the user just postponed.
        cur.execute(
            "UPDATE telegram_callback_tokens SET used_at=now() WHERE review_item_id=%s AND used_at IS NULL;",
            (item_id,),
        )
        cur.execute(
            """UPDATE telegram_control_surface_state
                  SET pending_question_review_item_id=NULL,
                      pending_question_source_sha256=NULL,
                      pending_question_expires_at=NULL,
                      pending_question_prompt_message_id=NULL,
                      updated_at=now()
                WHERE pending_question_review_item_id=%s;""",
            (item_id,),
        )
    return {"ok": True, "review_item_id": item_id, "application_id": row[0],
            "snoozed_until": row[1].isoformat() if row[1] else None}


def question_quick_choices(cur, item_id: str) -> list[str]:
    """Return conservative configured one-tap answers for a question item."""
    from services.review.ux_policy_v1 import quick_question_choices
    cur.execute(
        """SELECT h.payload_json
             FROM human_review_items h
            WHERE h.id=%s AND h.item_type='question_required'
              AND h.status IN ('pending','needs_revision');""", (item_id,)
    )
    row = cur.fetchone()
    if not row:
        return []
    question = str((row[0] or {}).get("question") or "")
    from services.common.immigration_semantics import legal_question_pause_reason
    if legal_question_pause_reason(question):
        return []
    # Discovery salary_floor is a user-owned configured number; use it only as
    # a convenience suggestion, never as an automatic answer.
    cur.execute("SELECT salary_floor FROM job_search_preferences WHERE profile_key='primary';")
    pref = cur.fetchone()
    salary_target = pref[0] if pref else None
    return quick_question_choices(question, salary_target=salary_target)


def document_change_summary(cur, item_id: str) -> list[str]:
    """Expose structured resume changes used by the canonical renderer."""
    from services.review.ux_policy_v1 import resume_change_lines
    cur.execute(
        """SELECT gd.doc_type, gd.evidence_map
             FROM human_review_items h
             JOIN generated_documents gd ON gd.id=h.generated_document_id
            WHERE h.id=%s AND h.item_type='document_review';""", (item_id,)
    )
    row = cur.fetchone()
    if not row or row[0] != 'resume':
        return []
    return resume_change_lines(dict(row[1] or {}))


def review_artifacts(cur, item_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """SELECT artifact_kind, file_path, filename, mime_type, sha256
             FROM human_review_artifacts WHERE review_item_id = %s ORDER BY created_at;""",
        (item_id,),
    )
    return [{"kind": r[0], "file_path": r[1], "filename": r[2], "mime_type": r[3], "sha256": r[4]}
            for r in cur.fetchall()]


def decide_item(conn, item_id: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
    if decision not in {"approve", "reject", "revise"}:
        raise ReviewError("decision must be approve, reject, or revise")
    review_decision = decision
    with conn.cursor() as cur:
        # Keep the lock order consistent with ensure_document_review():
        # application row first, review item second. Without this, concurrent
        # inbox sync + human approval can deadlock (app->item vs item->app).
        cur.execute(
            "SELECT application_id::text FROM human_review_items WHERE id = %s;",
            (item_id,),
        )
        seed = cur.fetchone()
        if not seed:
            raise ReviewError("Review item not found.")
        cur.execute("SELECT id::text FROM applications WHERE id = %s FOR UPDATE;", (seed[0],))
        if not cur.fetchone():
            raise ReviewError("Review item's application no longer exists.")
        cur.execute(
            """SELECT id::text, item_type, status, application_id::text,
                      generated_document_id::text, approval_request_id::text,
                      browser_task_id::text, source_sha256, reviewed_artifact_id::text, payload_json
                 FROM human_review_items WHERE id = %s FOR UPDATE;""",
            (item_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ReviewError("Review item not found.")
        if row[3] != seed[0]:
            raise ReviewError("Review item application binding changed concurrently.")
        (_id, item_type, status, app_id, document_id, approval_id, browser_task_id,
         source_sha, reviewed_artifact_id, item_payload) = row
        capability_result: dict[str, Any] | None = None
        materialized_approval_id: str | None = None
        materialized_approval_ids: list[str] = []
        reconciliation_outcome: str | None = None
        reconciliation_observed_result: dict[str, Any] | None = None
        workflow_followup_result: dict[str, Any] | None = None
        autofill_reprepare_required = False
        explicit_new_status: str | None = None
        if status not in {"pending", "needs_revision"}:
            raise ReviewError(f"Review item is already {status}.")

        if item_type == "document_review":
            cur.execute(
                """SELECT gd.content, gd.qa_status, gd.doc_type, gd.source_jd_hash, a.jd_hash
                     FROM generated_documents gd
                     JOIN applications a ON a.id = gd.application_id
                    WHERE gd.id = %s AND gd.application_id = %s
                    FOR UPDATE OF gd;""",
                (document_id, app_id),
            )
            doc = cur.fetchone()
            if not doc or _sha256_text(doc[0] or "") != source_sha:
                raise ReviewError("Document changed after review creation; sync a fresh review item.")
            cur.execute(
                """SELECT 1 FROM document_revision_requests
                     WHERE source_review_item_id=%s AND source_sha256=%s
                       AND status IN ('pending','running') LIMIT 1;""",
                (item_id, source_sha),
            )
            if cur.fetchone():
                raise ReviewError("The document agent has an active revision for this exact draft; wait for the fresh review card.")
            if decision == "approve":
                if doc[1] != "pass":
                    raise ReviewError("Only a QA-passed document may be approved.")
                if not doc[3] or not doc[4] or doc[3] != doc[4]:
                    raise ReviewError("The job description changed after this document was generated; regenerate and re-review it.")
                if not reviewed_artifact_id:
                    raise ReviewError("An exact reviewed PDF artifact is required before document approval.")
                cur.execute(
                    """SELECT hra.artifact_kind, hra.file_path, hra.sha256,
                              hra.generated_document_artifact_id::text, gda.generated_document_id::text
                         FROM human_review_artifacts hra
                         LEFT JOIN generated_document_artifacts gda ON gda.id = hra.generated_document_artifact_id
                        WHERE hra.id = %s AND hra.review_item_id = %s
                          AND gda.application_id = %s;""",
                    (reviewed_artifact_id, item_id, app_id),
                )
                artifact = cur.fetchone()
                doc_type = doc[2]
                required_kind = "resume_pdf" if doc_type == "resume" else "cover_letter_pdf"
                if (not artifact or artifact[0] != required_kind or artifact[4] != document_id
                        or not Path(artifact[1]).is_file() or _sha256_file(Path(artifact[1])) != artifact[2]):
                    raise ReviewError("The exact PDF artifact reviewed by the user is missing, changed, or unbound.")
                cur.execute("UPDATE generated_documents SET approved = true, approved_at = now() WHERE id = %s;", (document_id,))
                if doc_type == "resume":
                    cur.execute("""UPDATE applications SET approved_resume_id = %s,
                                  approved_resume_artifact_id = %s WHERE id = %s;""",
                                (document_id, artifact[3], app_id))
                    cur.execute("SELECT job_url, company, job_title FROM applications WHERE id=%s;", (app_id,))
                    app_handoff = cur.fetchone() or ("", "Unknown company", "Unknown role")
                    ensure_action_required_review(
                        cur, application_id=app_id, action_kind="open_apply_binding_required",
                        title="Open/focus the job page to prepare Apply",
                        summary=(f"{app_handoff[1]} — {app_handoff[2]}. Resume approval is complete. Open or focus "
                                 "the exact stored job page, then approve this handoff to create a fresh OPEN APPLY "
                                 "approval. No browser click occurs here."),
                        payload={"job_url": str(app_handoff[0] or ""), "resume_id": document_id,
                                 "resume_artifact_id": artifact[3]}, priority="high",
                    )
                elif doc_type == "cover_letter":
                    cur.execute("""UPDATE applications SET approved_cover_letter_id = %s,
                                  approved_cover_letter_artifact_id = %s WHERE id = %s;""",
                                (document_id, artifact[3], app_id))
            else:
                cur.execute("UPDATE generated_documents SET approved = false, approved_at = NULL WHERE id = %s;", (document_id,))
                if decision == "revise":
                    cur.execute(
                        """UPDATE human_review_items
                              SET payload_json = payload_json || %s, updated_at = now()
                            WHERE id = %s;""",
                        (Jsonb({"human_revision_required": True,
                                "human_revision_source_sha256": source_sha}), item_id),
                    )

        elif item_type == "approval_request":
            capability_decision = "reject" if decision == "revise" else decision
            if decision == "revise":
                note = (note + " Review requested revision; issue a fresh bound approval.").strip()
            from services.approval.approval_service_v1 import decide_request_by_id
            capability_result = decide_request_by_id(conn, approval_id, decision=capability_decision,
                                                     note=note, actor=actor, commit=False)
            if not capability_result.get("ok"):
                raise ReviewError(capability_result.get("error") or "Approval request decision failed.")

        elif item_type == "autofill_review":
            cur.execute("SELECT status, execution_state, result_json FROM browser_tasks WHERE id = %s;", (browser_task_id,))
            task = cur.fetchone()
            if not task or task[0] != "completed":
                raise ReviewError("Autofill task is no longer reviewable.")
            current_sha = _sha256_text(json.dumps(task[2] or {}, sort_keys=True, separators=(",", ":"), default=str))
            if source_sha and current_sha != source_sha:
                raise ReviewError("Autofill result changed after the review item was created.")
            if decision == "approve":
                if task[1] != "completed":
                    raise ReviewError("Only a fully completed deterministic autofill may be approved.")
                cur.execute(
                    """SELECT file_path, sha256 FROM human_review_artifacts
                         WHERE review_item_id = %s AND artifact_kind = 'autofill_screenshot'
                         ORDER BY created_at DESC LIMIT 1;""",
                    (item_id,),
                )
                screenshot = cur.fetchone()
                if screenshot:
                    screenshot_path = Path(screenshot[0]).expanduser()
                    if not screenshot_path.is_file() or _sha256_file(screenshot_path) != screenshot[1]:
                        raise ReviewError("Autofill screenshot artifact changed after review creation; capture a fresh artifact or remove the stale binding.")
                from services.control_plane.pipeline_state import DEFAULT_PIPELINE_STATE_STORE, PipelineStateError
                try:
                    DEFAULT_PIPELINE_STATE_STORE.transition(
                        cur, application_id=app_id, expected_from="form_filled", to="application_ready",
                        actor=actor,
                        reason="Human approved deterministic post-autofill state; every subsequent browser action requires a separate privileged approval.",
                        detail={"review_item_id": item_id, "browser_task_id": browser_task_id,
                                "screenshot_present": bool(screenshot)},
                        require_automated=False, allow_already_target=False,
                    )
                except PipelineStateError as exc:
                    raise ReviewError("Application is no longer at form_filled; a fresh human review is required.") from exc
                from services.application_actions.privileged_action_v1 import materialize_application_ready_gate
                try:
                    materialized_approval_ids = materialize_application_ready_gate(cur, app_id)
                    materialized_approval_id = materialized_approval_ids[0] if materialized_approval_ids else None
                except Exception as exc:
                    cur.execute(
                        """INSERT INTO application_events(application_id, event_type, event_source, event_payload)
                           VALUES (%s, 'application_ready_gate_materialization_failed', 'human_review_hub', %s);""",
                        (app_id, Jsonb({"review_item_id": item_id, "error": str(exc)[:1000]})),
                    )
                if materialized_approval_ids:
                    for candidate_approval_id in materialized_approval_ids:
                        ensure_approval_review(cur, candidate_approval_id)
                else:
                    ensure_application_ready_review(cur, app_id)
            elif decision == "revise":
                cur.execute("SELECT current_step FROM applications WHERE id=%s FOR UPDATE;", (app_id,))
                current = str((cur.fetchone() or ('',))[0] or '')
                if current not in {'awaiting_approval','form_filled'}:
                    raise ReviewError(f"Application is at {current!r}; cannot safely prepare a fresh autofill plan from this review.")
                from services.application_actions.privileged_action_v1 import _transition_application_step
                _transition_application_step(
                    cur, application_id=app_id, to_step='application_form_ready', actor=actor,
                    reason='Human requested a fresh deterministic autofill plan after reviewing the form.',
                    detail={"review_item_id": item_id, "browser_task_id": browser_task_id},
                )
                autofill_reprepare_required = True
                explicit_new_status = 'resolved'
                note = (note + " Fresh exact autofill plan requested; the previous capability remains retired.").strip()
            else:
                from services.application_actions.privileged_action_v1 import _transition_application_step
                _transition_application_step(
                    cur, application_id=app_id, to_step='abandoned', actor=actor,
                    reason='Human stopped the application from post-autofill review.',
                    detail={"review_item_id": item_id, "browser_task_id": browser_task_id}, status='abandoned',
                )
                explicit_new_status = 'rejected'

        elif item_type == "question_required":
            raise ReviewError("Question items require an explicit answer; use the answer command.")

        elif item_type == "sensitive_question_required":
            if decision != "approve":
                raise ReviewError("Sensitive legal questions cannot be dismissed. Use Later or stop the application from its workflow card.")
            question = str((item_payload or {}).get("question") or "")
            if not _sensitive_question_completed(
                cur, application_id=app_id, browser_task_id=str(browser_task_id or ''), question=question
            ):
                raise ReviewError("That exact sensitive question still appears unanswered in the bound JobOS form. Answer it there, then tap Done again.")
            cur.execute(
                """INSERT INTO application_events(application_id,event_type,event_source,event_payload)
                   VALUES (%s,'sensitive_question_manual_completed','human_review_hub',%s);""",
                (app_id, Jsonb({"review_item_id": item_id,
                                "question_class": str((item_payload or {}).get("question_class") or ""),
                                "browser_task_id": browser_task_id,
                                "actor": actor, "answer_stored": False})),
            )
            autofill_reprepare_required = _restore_form_ready_after_human_input(
                cur, application_id=app_id, browser_task_id=str(browser_task_id or ''), actor=actor,
                reason='Human completed every paused sensitive/runtime question; create a fresh exact autofill plan.',
            )
            note = (note + " Exact sensitive question was rechecked as completed; no legal answer was stored or autofilled.").strip()

        elif item_type == "reconciliation_required":
            privileged_execution_id = str((item_payload or {}).get("privileged_execution_id") or "").strip()
            if privileged_execution_id:
                if decision == "revise":
                    raise ReviewError("Privileged reconciliation has exactly two outcomes: occurred or not_occurred.")
                cur.execute(
                    """SELECT status, action_type, approval_request_id::text, result_json
                         FROM privileged_action_executions
                        WHERE id = %s AND application_id = %s FOR UPDATE;""",
                    (privileged_execution_id, app_id),
                )
                execution = cur.fetchone()
                if not execution or execution[0] != "needs_reconciliation":
                    raise ReviewError("Privileged execution is no longer awaiting reconciliation; sync the inbox.")
                action_type = execution[1]
                reconciliation_outcome = "occurred" if decision == "approve" else "not_occurred"
                reconciled_result = dict(execution[3] or {})
                if reconciliation_outcome == "occurred":
                    from services.application_actions.privileged_action_v1 import reconcile_observed_privileged_effect
                    try:
                        reconciliation_observed_result = reconcile_observed_privileged_effect(
                            cur, application_id=app_id, approval_request_id=str(execution[2]),
                            action_type=str(action_type),
                        )
                    except Exception as exc:
                        raise ReviewError(
                            "Human reported OCCURRED, but JobOS could not reconstruct the current browser/application state safely. "
                            f"Refocus the application tab and retry reconciliation: {exc}"
                        ) from exc
                reconciled_result.update({"reconciliation": reconciliation_outcome,
                                          "reconciled_by": actor, "review_item_id": item_id,
                                          "observed": reconciliation_observed_result or {}})
                new_execution_status = "completed" if reconciliation_outcome == "occurred" else "failed"
                cur.execute(
                    """UPDATE privileged_action_executions
                          SET status = %s, result_json = %s, error_message = NULL, finished_at = now()
                        WHERE id = %s;""",
                    (new_execution_status, Jsonb(reconciled_result), privileged_execution_id),
                )
                cur.execute(
                    """INSERT INTO application_events(application_id, event_type, event_source, event_payload)
                       VALUES (%s, 'privileged_action_reconciled', 'human_review_hub', %s);""",
                    (app_id, Jsonb({"privileged_execution_id": privileged_execution_id,
                                    "action_type": action_type, "outcome": reconciliation_outcome,
                                    "actor": actor})),
                )
            else:
                cur.execute("SELECT execution_state FROM browser_tasks WHERE id = %s;", (browser_task_id,))
                task = cur.fetchone()
                if task and task[0] == "needs_reconciliation":
                    if decision != "approve":
                        raise ReviewError(
                            "Inspect the form first, then use ‘I inspected the form’ to close this uncertain autofill safely."
                        )
                    # Reuse the canonical reconciliation close path so Telegram
                    # cannot accidentally make an uncertain capability replayable.
                    # The human has explicitly inspected the browser state; this
                    # retires the old task and restores a fresh-approval boundary.
                    from services.autofill.autofill_reconcile_v1 import close as close_autofill_reconciliation
                    try:
                        close_autofill_reconciliation(cur, str(browser_task_id))
                    except SystemExit as exc:
                        raise ReviewError(str(exc)) from exc
                    reconciliation_outcome = "autofill_inspected_closed"

        elif item_type == "action_required":
            payload = dict(item_payload or {})
            action_kind = str(payload.get("action_kind") or "")
            candidate_id = str(payload.get("candidate_id") or "")
            if decision == "approve":
                try:
                    from services.application_actions.privileged_action_v1 import prepare as prepare_privileged
                    if action_kind == "open_apply_binding_required":
                        cur.execute("SELECT coalesce(job_url, '') FROM applications WHERE id=%s;", (app_id,))
                        current_job = cur.fetchone()
                        stored_job_url = str(current_job[0] if current_job else "")
                        handoff_job_url = str(payload.get("job_url") or "")
                        if not stored_job_url or handoff_job_url != stored_job_url:
                            raise ReviewError("The stored job page changed; JobOS issued no browser action. Sync the newest handoff card.")
                        _focus_or_open_exact_job_page(stored_job_url)
                        materialized_approval_id = prepare_privileged(
                            cur, application_id=app_id, action="begin_application"
                        )
                    elif action_kind in {"email_verification_binding_required",
                                         "email_verification_candidate_ambiguity"}:
                        if not candidate_id:
                            raise ReviewError("email-verification handoff is missing candidate_id")

                        # A generic/ambiguous magic-link email has two independent
                        # authorities: trust the exact link domain, then use the
                        # exact verification candidate. Never package USE EMAIL
                        # first and let it fail later because link trust is absent.
                        cur.execute(
                            """SELECT gmail_account, gmail_message_id, sender, subject, verification_kind,
                                      secret_sha256, secret_context_json
                                 FROM email_verification_candidates
                                WHERE id=%s AND application_id=%s
                                  AND status IN ('discovered','approved');""",
                            (candidate_id, app_id),
                        )
                        email_candidate = cur.fetchone()
                        if not email_candidate:
                            raise ReviewError("email-verification candidate is no longer available")

                        if str(email_candidate[4]) == "magic_link":
                            from urllib.parse import urlsplit
                            from services.application_actions.action_request_v1 import create_privileged_request
                            from services.application_actions.privileged_action_v1 import _host_is_allowed

                            secret_context = dict(email_candidate[6] or {})
                            link_origin = str(secret_context.get("link_origin") or "")
                            link_host = (urlsplit(link_origin).hostname or "").casefold()
                            if not link_origin or not link_host:
                                raise ReviewError("magic-link candidate is missing its exact link origin")
                            if not _host_is_allowed(
                                cur, link_origin, application_id=app_id, purpose="gmail_magic_link"
                            ):
                                materialized_approval_id = create_privileged_request(
                                    cur, application_id=app_id, action_type="privileged_trust_external_domain",
                                    payload={
                                        "domain": link_host, "expected_origin": link_origin,
                                        "trust_source": "gmail_magic_link", "candidate_id": candidate_id,
                                        "gmail_account": email_candidate[0],
                                        "gmail_message_id": email_candidate[1],
                                        "verification_kind": email_candidate[4],
                                        "secret_sha256": email_candidate[5],
                                        "secret_context": secret_context,
                                        "review_context": {"screenshot_path": "NaN"},
                                    },
                                    summary=(f"Trust email-verification link domain {link_host} before JobOS opens "
                                             "the exact approved magic link."),
                                    requested_by="human-review-handoff",
                                )
                                ensure_approval_review(cur, materialized_approval_id)
                                ensure_action_required_review(
                                    cur, application_id=app_id,
                                    action_kind="email_verification_binding_required",
                                    title="Magic link trust pending — then bind verification",
                                    summary=("Approve the separate magic-link domain trust first. Then open/refocus the "
                                             "employer verification page and approve this handoff to create a fresh exact "
                                             "USE EMAIL VERIFICATION approval. No secret is stored in this review item."),
                                    payload={
                                        "candidate_id": candidate_id,
                                        "gmail_message_id": email_candidate[1],
                                        "verification_kind": email_candidate[4],
                                    },
                                    priority="urgent",
                                )
                                note = (note + " Separate Gmail magic-link trust was materialized first; USE EMAIL remains gated until that trust exists.").strip()
                            else:
                                materialized_approval_id = prepare_privileged(
                                    cur, application_id=app_id, action="use_email_verification", candidate_id=candidate_id
                                )
                        else:
                            materialized_approval_id = prepare_privileged(
                                cur, application_id=app_id, action="use_email_verification", candidate_id=candidate_id
                            )
                    elif action_kind == "workflow_followup_required":
                        expected_step = str(payload.get("expected_step") or "")
                        cur.execute("SELECT current_step FROM applications WHERE id=%s;", (app_id,))
                        current = cur.fetchone()
                        if not current or str(current[0]) != expected_step:
                            raise ReviewError("workflow state changed; sync a fresh next-gate handoff")
                        from services.application_actions.privileged_action_v1 import (
                            _transport, _snapshot, detect_page_state, detect_platform, _update_auth_session,
                        )
                        from services.common.application_browser_binding_v1 import (
                            ApplicationBrowserBindingError, resolve_application_bound_target,
                        )
                        transport = _transport()
                        expected_url = str(payload.get("expected_url") or "")
                        try:
                            bound = resolve_application_bound_target(
                                cur, transport, application_id=app_id,
                                allow_focused_rebind=not bool(str(payload.get("target_id") or "")),
                                expected_url=expected_url,
                            )
                        except ApplicationBrowserBindingError as exc:
                            raise ReviewError(str(exc)) from exc
                        target_id = bound.target_id
                        live_url, live_snap, live_nodes, live_fp = _snapshot(transport, target_id)
                        live_state, live_detail = detect_page_state(live_url, live_snap, live_nodes)
                        if live_state != expected_step:
                            raise ReviewError(
                                f"live page classifies as {live_state!r}, not authoritative {expected_step!r}; resync auth state first"
                            )
                        platform = detect_platform(live_url, live_snap)
                        _update_auth_session(
                            cur, application_id=app_id, url=live_url, fingerprint=live_fp,
                            state=live_state, platform=platform,
                            detail={**live_detail, "target_id": target_id, "human_refocus": True},
                        )
                        workflow_followup_result = {
                            "target_id": target_id, "url": live_url, "state": live_state,
                            "detail": live_detail, "page_fingerprint": live_fp, "followup": "state_gate",
                        }
                        note = (note + " Fresh page snapshot captured; next gate will be materialized after this review decision commits.").strip()
                    else:
                        raise ReviewError(f"unsupported action-required kind: {action_kind}")
                except ReviewError:
                    raise
                except Exception as exc:
                    raise ReviewError(
                        f"Fresh browser binding is not ready yet: {str(exc)[:500]}. Refocus the exact page and retry this review item."
                    ) from exc
                if materialized_approval_id:
                    ensure_approval_review(cur, materialized_approval_id)
                    note = (note + " Fresh exact-bound privileged approval prepared separately; this handoff performed no browser I/O.").strip()
            elif decision == "reject":
                if candidate_id and action_kind.startswith("email_verification_"):
                    cur.execute(
                        "UPDATE email_verification_candidates SET status='rejected' WHERE id=%s AND application_id=%s AND status IN ('discovered','approved');",
                        (candidate_id, app_id),
                    )
                elif not action_kind.startswith("email_verification_"):
                    raise ReviewError("This handoff is still required by the application state. Use Later to postpone it; it cannot be dismissed permanently.")
                note = (note + " Handoff dismissed; no browser action executed.").strip()
            else:
                note = (note + " Refocus/fix the browser context, then retry this handoff.").strip()

        elif item_type == "application_ready":
            if decision == "approve":
                from services.application_actions.privileged_action_v1 import materialize_application_ready_gate
                materialized_approval_ids = materialize_application_ready_gate(cur, app_id)
                materialized_approval_id = materialized_approval_ids[0] if materialized_approval_ids else None
                if not materialized_approval_ids:
                    raise ReviewError("The fresh page has no unambiguous exact candidate gate. Refocus the application tab or inspect the page before trying again.")
                for candidate_approval_id in materialized_approval_ids:
                    ensure_approval_review(cur, candidate_approval_id)
                if len(materialized_approval_ids) > 1:
                    note = (note + " Multiple exact candidate gates were prepared separately; choose the intended Telegram approval. No browser action executed.").strip()
                else:
                    note = (note + " Prepared a fresh exact-bound browser approval; no browser action executed.").strip()
            elif decision == "reject":
                from services.application_actions.privileged_action_v1 import _transition_application_step
                _transition_application_step(
                    cur, application_id=app_id, to_step="abandoned", actor=actor,
                    reason="Human declined to continue from application_ready.",
                    detail={"review_item_id": item_id}, status="abandoned",
                )
            else:
                note = (note + " Keep application_ready pending for manual inspection and fresh gate preparation.").strip()

        new_status = explicit_new_status or {"approve": "approved", "reject": "rejected", "revise": "needs_revision"}[review_decision]
        cur.execute(
            """UPDATE human_review_items
                  SET status = %s, decided_by = %s, decision_note = %s,
                      decided_at = now(), updated_at = now()
                WHERE id = %s;""",
            (new_status, actor, note, item_id),
        )
        cur.execute(
            """UPDATE review_bundles rb SET updated_at = now(), status = CASE
                   WHEN EXISTS (SELECT 1 FROM human_review_items x WHERE x.review_bundle_id = rb.id AND x.status IN ('pending','needs_revision')) THEN 'in_review'
                   WHEN EXISTS (SELECT 1 FROM human_review_items x WHERE x.review_bundle_id = rb.id AND x.status = 'rejected') THEN 'rejected'
                   ELSE 'approved' END
                WHERE id = (SELECT review_bundle_id FROM human_review_items WHERE id = %s);""",
            (item_id,),
        )
    response = {"ok": True, "review_item_id": item_id, "status": new_status, "decision": review_decision,
                "application_id": app_id, "item_type": item_type}
    if capability_result:
        response["approval_request_id"] = approval_id
        response["approval_type"] = capability_result.get("type")
        response["delegated_to_autofill"] = bool(capability_result.get("delegated_to_autofill"))
        response["autofill_queued"] = bool(capability_result.get("autofill_queued"))
    if materialized_approval_id:
        response["materialized_approval_request_id"] = materialized_approval_id
    if materialized_approval_ids:
        response["materialized_approval_request_ids"] = materialized_approval_ids
    if reconciliation_outcome:
        response["reconciliation_outcome"] = reconciliation_outcome
    if reconciliation_observed_result:
        response["reconciliation_observed_result"] = reconciliation_observed_result
    if workflow_followup_result:
        response["post_commit_followup_result"] = workflow_followup_result
    if autofill_reprepare_required:
        response["autofill_reprepare_required"] = True
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS unified human review inbox")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync")
    sub.add_parser("inbox")
    show = sub.add_parser("show"); show.add_argument("item_id")
    for name in ("approve", "reject", "revise"):
        item = sub.add_parser(name); item.add_argument("item_id"); item.add_argument("--note", default=""); item.add_argument("--actor", default="user")
    answer = sub.add_parser("answer"); answer.add_argument("item_id"); answer.add_argument("--text", required=True)
    answer.add_argument("--scope", choices=("company", "ats", "global"), default="company")
    answer.add_argument("--answer-kind", choices=("text", "option"), default="text")
    answer.add_argument("--actor", default="user")
    feedback = sub.add_parser("feedback"); feedback.add_argument("item_id"); feedback.add_argument("--text", required=True)
    feedback.add_argument("--actor", default="user")
    args = parser.parse_args()
    with psycopg.connect(DSN, autocommit=False) as conn:
        if args.command == "sync":
            with conn.cursor() as cur:
                result = sync_inbox(cur)
            conn.commit(); print(json.dumps(result, indent=2)); return 0
        if args.command == "inbox":
            with conn.cursor() as cur:
                sync_inbox(cur); rows = list_inbox(cur)
            conn.commit(); print(json.dumps(rows, indent=2)); return 0
        if args.command == "show":
            with conn.cursor() as cur:
                cur.execute("SELECT row_to_json(v) FROM v_human_review_inbox v WHERE review_item_id = %s;", (args.item_id,))
                row = cur.fetchone()
                if not row:
                    raise ReviewError("Review item is not actionable or does not exist.")
                payload = row[0]; payload["artifacts"] = review_artifacts(cur, args.item_id)
            conn.rollback(); print(json.dumps(payload, indent=2, default=str)); return 0
        if args.command == "answer":
            out = answer_question(conn, args.item_id, answer=args.text, actor=args.actor,
                                  scope=args.scope, answer_kind=args.answer_kind)
            conn.commit()
            if out.get("autofill_reprepare_required"):
                out["autofill_reprepare"] = prepare_fresh_autofill_after_human_input(str(out["application_id"]))
            print(json.dumps(out, indent=2)); return 0
        if args.command == "feedback":
            out = submit_document_feedback(conn, args.item_id, feedback=args.text, actor=args.actor)
            conn.commit(); print(json.dumps(out, indent=2)); return 0
        out = decide_item(conn, args.item_id, decision=args.command, actor=args.actor, note=args.note)
        conn.commit()
        if out.get("autofill_reprepare_required"):
            out["autofill_reprepare"] = prepare_fresh_autofill_after_human_input(str(out["application_id"]))
        observed = out.get("reconciliation_observed_result") or {}
        followup_source = out.get("post_commit_followup_result") or observed
        if followup_source.get("state") and followup_source.get("state") != "submitted":
            try:
                from services.application_actions.privileged_action_v1 import _post_commit_followup
                out["post_commit_followup"] = _post_commit_followup(conn, out["application_id"], followup_source)
            except Exception as exc:
                out["post_commit_followup"] = {"ok": False, "error": str(exc)[:1000]}
        print(json.dumps(out, indent=2)); return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReviewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
