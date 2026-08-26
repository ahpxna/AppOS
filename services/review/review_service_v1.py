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
                  ar.token_expires_at, a.company, a.job_title
             FROM approval_requests ar LEFT JOIN applications a ON a.id = ar.application_id
            WHERE ar.id = %s AND ar.status = 'pending'
              AND ar.token_expires_at > now();""",
        (approval_request_id,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    app_id, approval_type, _status, summary, expires, company, role = row
    bundle_id = ensure_bundle(cur, app_id)
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, approval_request_id,
               title, summary_text, priority, payload_json)
           VALUES (%s, %s, 'approval_request', %s, %s, %s, 'urgent', %s)
           ON CONFLICT (approval_request_id)
             WHERE approval_request_id IS NOT NULL AND item_type = 'approval_request' AND status = 'pending'
           DO UPDATE SET title = EXCLUDED.title, summary_text = EXCLUDED.summary_text,
                         payload_json = EXCLUDED.payload_json, updated_at = now()
           RETURNING id::text;""",
        (bundle_id, app_id, approval_request_id, f"Approval required: {approval_type}",
         summary or f"{company or ''} — {role or ''}",
         Jsonb({"approval_type": approval_type, "expires_at": expires.isoformat() if expires else None})),
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
                   'Final human submission required', %s, 'urgent', %s)
           ON CONFLICT (application_id)
             WHERE item_type = 'application_ready' AND status = 'pending'
           DO UPDATE SET summary_text = EXCLUDED.summary_text,
                         payload_json = EXCLUDED.payload_json, updated_at = now()
           RETURNING id::text;""",
        (bundle_id, application_id,
         f"{company} — {role}. Autofill review is complete, but the next browser gate could not be classified automatically. Approve this item only to prepare a fresh bound Next/Consent/Submit approval; it does not perform the browser action.",
         Jsonb({"submit": "separate_privileged_gate", "browser_action_authorized": False,
                "approve_semantics": "prepare_next_gate_only"})),
    )
    return str(cur.fetchone()[0])


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
    from services.common.immigration_semantics import legal_question_pause_reason
    question = (question or "").strip()
    normalized = normalize_question(question)
    if not normalized or legal_question_pause_reason(question):
        return None
    cur.execute("SELECT company, job_title FROM applications WHERE id = %s;", (application_id,))
    app = cur.fetchone()
    if not app:
        return None
    source_sha = _sha256_text(f"runtime|{application_id}|{browser_task_id}|{normalized}")
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
        """SELECT id::text, application_id::text, result_json
             FROM browser_tasks
            WHERE task_type = 'fill_application_form' AND status = 'completed'
              AND jsonb_typeof(coalesce(result_json, '{}'::jsonb)->'paused_fields') = 'array';"""
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
    normalized = normalize_question(question)
    if not normalized:
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
    return {"ok": True, "review_item_id": item_id, "status": "resolved",
            "scope": scope, "question": normalized, "answer_kind": answer_kind}


def reconcile_materialized_review_state(cur) -> None:
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


def sync_inbox(cur) -> dict[str, int]:
    reconcile_materialized_review_state(cur)
    counts = {"documents": 0, "questions": 0, "approvals": 0,
              "autofill": 0, "reconciliation": 0, "application_ready": 0}
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
    for (app_id,) in cur.fetchall():
        if ensure_application_ready_review(cur, app_id):
            counts["application_ready"] += 1
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
            if decision == "approve":
                cur.execute("SELECT status, execution_state, result_json FROM browser_tasks WHERE id = %s;", (browser_task_id,))
                task = cur.fetchone()
                if not task or task[0] != "completed" or task[1] != "completed":
                    raise ReviewError("Only a fully completed deterministic autofill may be approved.")
                current_sha = _sha256_text(json.dumps(task[2] or {}, sort_keys=True, separators=(",", ":"), default=str))
                if source_sha and current_sha != source_sha:
                    raise ReviewError("Autofill result changed after the review item was created.")
                cur.execute(
                    """SELECT file_path, sha256 FROM human_review_artifacts
                         WHERE review_item_id = %s AND artifact_kind = 'autofill_screenshot'
                         ORDER BY created_at DESC LIMIT 1;""",
                    (item_id,),
                )
                screenshot = cur.fetchone()
                # Screenshot capture is best-effort. A missing screenshot must not
                # invalidate an otherwise durable, deterministic browser completion.
                # If a screenshot exists, however, its exact bytes remain review-bound.
                if screenshot:
                    screenshot_path = Path(screenshot[0]).expanduser()
                    if not screenshot_path.is_file() or _sha256_file(screenshot_path) != screenshot[1]:
                        raise ReviewError("Autofill screenshot artifact changed after review creation; capture a fresh artifact or remove the stale binding.")
                cur.execute("UPDATE applications SET current_step = 'application_ready', updated_at = now() WHERE id = %s AND current_step = 'form_filled';", (app_id,))
                transitioned = cur.rowcount == 1
                if not transitioned:
                    raise ReviewError(
                        "Application is no longer at form_filled; a fresh human review is required."
                    )
                cur.execute(
                    """INSERT INTO pipeline_events(application_id, from_step, to_step, actor, reason, detail_json)
                       VALUES (%s, 'form_filled', 'application_ready', %s,
                               'Human approved deterministic post-autofill state; every subsequent browser action requires a separate privileged approval.', %s);""",
                    (app_id, actor, Jsonb({"review_item_id": item_id, "browser_task_id": browser_task_id,
                                          "screenshot_present": bool(screenshot)})),
                )
                from services.application_actions.privileged_action_v1 import materialize_application_ready_gate
                try:
                    materialized_approval_ids = materialize_application_ready_gate(cur, app_id)
                    materialized_approval_id = materialized_approval_ids[0] if materialized_approval_ids else None
                except Exception as exc:
                    # Classification/package failure is not permission to submit. Keep
                    # application_ready visible so the user can retry gate preparation.
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

        elif item_type == "question_required":
            raise ReviewError("Question items require an explicit answer; use the answer command.")

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
                    raise ReviewError(
                        "Underlying autofill task still needs reconciliation; close it with autofill_reconcile_v1 first. "
                        "Reject/revise cannot dismiss an uncertain browser side effect."
                    )

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

        new_status = {"approve": "approved", "reject": "rejected", "revise": "needs_revision"}[review_decision]
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
            conn.commit(); print(json.dumps(out, indent=2)); return 0
        out = decide_item(conn, args.item_id, decision=args.command, actor=args.actor, note=args.note)
        conn.commit()
        observed = out.get("reconciliation_observed_result") or {}
        if observed.get("state") and observed.get("state") != "submitted":
            try:
                from services.application_actions.privileged_action_v1 import _post_commit_followup
                out["reconciliation_followup"] = _post_commit_followup(conn, out["application_id"], observed)
            except Exception as exc:
                out["reconciliation_followup"] = {"ok": False, "error": str(exc)[:1000]}
        print(json.dumps(out, indent=2)); return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReviewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
