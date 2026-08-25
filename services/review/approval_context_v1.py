"""Best-effort context envelopes for every JobOS human approval surface.

Delivery is intentionally soft-fail: an unavailable section becomes ``"NaN"``
instead of preventing the Telegram message or approval controls from rendering.
This is a presentation contract only. Executors still fail closed immediately
before any real side effect when an exact binding is unavailable or changed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from psycopg.types.json import Jsonb

NAN = "NaN"


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        value = fn()
        return NAN if value is None else value
    except Exception:
        return NAN


def _safe_db(cur, fn: Callable[[], Any]) -> Any:
    """Soft-fail one DB-backed context section without poisoning the transaction.

    PostgreSQL marks the whole transaction aborted after a statement error. A
    plain try/except is therefore insufficient for the Telegram soft-fail
    contract. Each optional section gets its own savepoint; only that section
    rolls back and becomes NaN.
    """
    cur.execute("SAVEPOINT jobos_context_softfail")
    try:
        value = fn()
        cur.execute("RELEASE SAVEPOINT jobos_context_softfail")
        return NAN if value is None else value
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT jobos_context_softfail")
        cur.execute("RELEASE SAVEPOINT jobos_context_softfail")
        return NAN


def _application(cur, application_id: str) -> dict[str, Any]:
    cur.execute(
        """SELECT id::text, company, job_title, job_url, location, work_mode, seniority_level,
                  fit_score, fit_decision, current_step, status, coalesce(ats_type,'unknown'),
                  jd_hash, jd_text
             FROM applications WHERE id = %s;""",
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("application missing")
    return {
        "application_id": row[0], "company": row[1] or NAN, "job_title": row[2] or NAN,
        "job_url": row[3] or NAN, "location": row[4] or NAN, "work_mode": row[5] or NAN,
        "seniority": row[6] or NAN, "fit_score": row[7] if row[7] is not None else NAN,
        "fit_decision": row[8] or NAN, "current_step": row[9] or NAN, "status": row[10] or NAN,
        "ats_type": row[11] or NAN, "jd_sha256": row[12] or NAN,
        "jd_text": row[13] or NAN,
    }


def _fit_analysis(cur, application_id: str) -> dict[str, Any]:
    cur.execute(
        """SELECT fit_score, fit_decision, decision_reason, matched_requirements,
                  missing_or_weak_requirements, hard_blockers, risk_flags, quick_learn_targets,
                  analyzer_version, analyzer_model, created_at
             FROM job_fit_analyses
            WHERE application_id = %s
            ORDER BY created_at DESC LIMIT 1;""",
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"fit_score": NAN, "fit_decision": NAN, "decision_reason": NAN,
                "matched_requirements": NAN, "missing_or_weak_requirements": NAN,
                "hard_blockers": NAN, "risk_flags": NAN, "quick_learn_targets": NAN}
    return {
        "fit_score": row[0] if row[0] is not None else NAN,
        "fit_decision": row[1] or NAN,
        "decision_reason": row[2] or NAN,
        "matched_requirements": row[3] if row[3] is not None else NAN,
        "missing_or_weak_requirements": row[4] if row[4] is not None else NAN,
        "hard_blockers": row[5] if row[5] is not None else NAN,
        "risk_flags": row[6] if row[6] is not None else NAN,
        "quick_learn_targets": row[7] if row[7] is not None else NAN,
        "analyzer_version": row[8] or NAN, "analyzer_model": row[9] or NAN,
        "created_at": row[10].isoformat() if row[10] else NAN,
    }


def _documents(cur, application_id: str) -> dict[str, Any]:
    cur.execute(
        """SELECT a.approved_resume_id::text, a.approved_cover_letter_id::text,
                  a.approved_resume_artifact_id::text, a.approved_cover_letter_artifact_id::text
             FROM applications a WHERE a.id = %s;""",
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("application missing")
    result: dict[str, Any] = {}
    for name, document_id, artifact_id in (("resume", row[0], row[2]), ("cover_letter", row[1], row[3])):
        if not document_id:
            result[name] = NAN
            continue
        cur.execute(
            """SELECT gd.id::text, gd.version, gd.qa_status, gd.approved, gd.content,
                      gda.id::text, gda.file_path, gda.filename, gda.sha256
                 FROM generated_documents gd
                 LEFT JOIN generated_document_artifacts gda ON gda.id = %s
                WHERE gd.id = %s AND gd.application_id = %s;""",
            (artifact_id, document_id, application_id),
        )
        doc = cur.fetchone()
        if not doc:
            result[name] = NAN
            continue
        result[name] = {
            "document_id": doc[0], "version": doc[1], "qa_status": doc[2] or NAN,
            "approved": bool(doc[3]), "content_preview": (doc[4] or "")[:1800] or NAN,
            "artifact_id": doc[5] or NAN, "file_path": doc[6] or NAN,
            "filename": doc[7] or NAN, "sha256": doc[8] or NAN,
        }
    return result


def _auth(cur, application_id: str) -> dict[str, Any]:
    cur.execute(
        """SELECT employer_origin, account_email, platform_hint, auth_state, current_url,
                  page_fingerprint, last_event, detail_json, updated_at
             FROM application_auth_sessions WHERE application_id = %s;""",
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"state": NAN, "employer_origin": NAN, "account_email": NAN, "platform_hint": NAN}
    return {
        "employer_origin": row[0] or NAN, "account_email": row[1] or NAN,
        "platform_hint": row[2] or NAN, "state": row[3] or NAN,
        "current_url": row[4] or NAN, "page_fingerprint": row[5] or NAN,
        "last_event": row[6] or NAN, "detail": row[7] or {},
        "updated_at": row[8].isoformat() if row[8] else NAN,
    }


def _latest_browser(cur, application_id: str) -> dict[str, Any]:
    cur.execute(
        """SELECT id::text, task_type, status, execution_state, pinned_target_id,
                  expected_initial_url, expected_page_fingerprint, result_json, screenshot_url, finished_at
             FROM browser_tasks WHERE application_id = %s
            ORDER BY coalesce(finished_at, started_at, created_at) DESC LIMIT 1;""",
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"task": NAN, "target_url": NAN, "screenshot": NAN}
    return {
        "browser_task_id": row[0], "task_type": row[1], "status": row[2],
        "execution_state": row[3], "target_id": row[4] or NAN,
        "target_url": row[5] or NAN, "page_fingerprint": row[6] or NAN,
        "result": row[7] or {}, "screenshot": row[8] or NAN,
        "finished_at": row[9].isoformat() if row[9] else NAN,
    }


def _approval(cur, review_item_id: str) -> dict[str, Any]:
    cur.execute(
        """SELECT h.item_type, h.payload_json, h.approval_request_id::text,
                  ar.type, ar.status, ar.payload_json, ar.summary_text, ar.token_expires_at
             FROM human_review_items h
             LEFT JOIN approval_requests ar ON ar.id = h.approval_request_id
            WHERE h.id = %s;""",
        (review_item_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("review item missing")
    return {
        "review_item_type": row[0], "review_payload": row[1] or {},
        "approval_request_id": row[2] or NAN, "action_type": row[3] or row[0] or NAN,
        "approval_status": row[4] or NAN, "action_payload": row[5] or {},
        "summary": row[6] or NAN, "expires_at": row[7].isoformat() if row[7] else NAN,
    }


def _form_context(approval: dict[str, Any], browser: Any) -> dict[str, Any]:
    payload = approval.get("action_payload") if isinstance(approval, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    review_context = payload.get("review_context") if isinstance(payload.get("review_context"), dict) else {}
    return {
        "proposed_fields": review_context.get("write_actions", payload.get("field_plan", NAN)),
        "paused_fields": review_context.get("will_pause", payload.get("paused_fields", NAN)),
        "consent_items": payload.get("consent_items", NAN),
        "required_blockers": payload.get("required_blockers", NAN),
        "uploads": review_context.get("uploads", payload.get("uploads", NAN)),
        "latest_browser_result": browser.get("result", NAN) if isinstance(browser, dict) else NAN,
    }


def build_envelope(cur, review_item_id: str, application_id: str) -> dict[str, Any]:
    """Build every section independently so one broken source cannot suppress a review."""
    approval = _safe_db(cur, lambda: _approval(cur, review_item_id))
    browser = _safe_db(cur, lambda: _latest_browser(cur, application_id))
    if isinstance(approval, dict) and isinstance(browser, dict):
        action_payload = approval.get("action_payload") if isinstance(approval.get("action_payload"), dict) else {}
        review_context = action_payload.get("review_context") if isinstance(action_payload.get("review_context"), dict) else {}
        if action_payload.get("expected_url"):
            browser["target_url"] = action_payload.get("expected_url")
        if action_payload.get("target_id"):
            browser["target_id"] = action_payload.get("target_id")
        if action_payload.get("expected_page_fingerprint"):
            browser["page_fingerprint"] = action_payload.get("expected_page_fingerprint")
        if review_context.get("screenshot_path") and review_context.get("screenshot_path") != NAN:
            browser["screenshot"] = review_context.get("screenshot_path")
    envelope = {
        "schema": "jobos-human-approval-envelope-v1",
        "job": _safe_db(cur, lambda: _application(cur, application_id)),
        "fit": _safe_db(cur, lambda: _fit_analysis(cur, application_id)),
        "approval": approval,
        "browser": browser,
        "documents": _safe_db(cur, lambda: _documents(cur, application_id)),
        "form": _safe(lambda: _form_context(approval, browser)),
        "auth": _safe_db(cur, lambda: _auth(cur, application_id)),
    }
    return envelope


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key in sorted(value):
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(value[key], path))
    elif isinstance(value, list):
        out[prefix] = value
    else:
        out[prefix] = value
    return out


VOLATILE_DIFF_PATHS = {
    "approval.approval_request_id", "approval.approval_status", "approval.expires_at",
    "approval.review_payload.expires_at", "browser.finished_at", "auth.updated_at",
}


def context_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    if not previous:
        return {"baseline": True, "changed": []}
    old, new = _flatten(previous), _flatten(current)
    changed = []
    for key in sorted(set(old) | set(new)):
        if key in VOLATILE_DIFF_PATHS:
            continue
        if old.get(key, NAN) != new.get(key, NAN):
            changed.append({"path": key, "before": old.get(key, NAN), "after": new.get(key, NAN)})
    return {"baseline": False, "changed": changed}


def snapshot_context(cur, *, review_item_id: str, application_id: str,
                     action_scope: str, envelope: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Persist the message snapshot and diff against the previous same app/action package."""
    cur.execute(
        """SELECT s.context_json
             FROM approval_context_snapshots s
             JOIN telegram_review_deliveries d
               ON d.review_item_id = s.review_item_id
              AND d.context_sha256 = s.context_sha256
              AND d.delivery_kind = 'summary'
              AND d.status = 'sent'
            WHERE s.application_id = %s AND s.action_scope = %s
            ORDER BY d.delivered_at DESC LIMIT 1;""",
        (application_id, action_scope),
    )
    row = cur.fetchone()
    previous = row[0] if row and isinstance(row[0], dict) else None
    diff = context_diff(previous, envelope)
    digest = _sha(envelope)
    cur.execute(
        """SELECT diff_json FROM approval_context_snapshots
             WHERE review_item_id = %s AND context_sha256 = %s
             ORDER BY created_at DESC LIMIT 1;""",
        (review_item_id, digest),
    )
    existing = cur.fetchone()
    if existing:
        return digest, existing[0] if isinstance(existing[0], dict) else diff
    cur.execute(
        """INSERT INTO approval_context_snapshots(
               review_item_id, application_id, action_scope, context_sha256, context_json, diff_json)
           VALUES (%s, %s, %s, %s, %s, %s);""",
        (review_item_id, application_id, action_scope, digest, Jsonb(envelope), Jsonb(diff)),
    )
    return digest, diff


def context_files(envelope: dict[str, Any]) -> list[dict[str, str]]:
    """Return best-effort local document/screenshot attachments without requiring them."""
    files: list[dict[str, str]] = []
    documents = envelope.get("documents") if isinstance(envelope, dict) else {}
    if isinstance(documents, dict):
        for kind in ("resume", "cover_letter"):
            item = documents.get(kind)
            if not isinstance(item, dict):
                continue
            path = item.get("file_path")
            if isinstance(path, str) and path != NAN and Path(path).is_file():
                files.append({"kind": kind, "path": path, "filename": str(item.get("filename") or Path(path).name),
                              "sha256": str(item.get("sha256") or NAN), "mime_type": "application/pdf"})
    browser = envelope.get("browser") if isinstance(envelope, dict) else {}
    if isinstance(browser, dict):
        path = browser.get("screenshot")
        if isinstance(path, str) and path != NAN and Path(path).is_file():
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            files.append({"kind": "screenshot", "path": path, "filename": Path(path).name,
                          "sha256": digest, "mime_type": "image/png"})
    return files
