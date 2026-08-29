#!/usr/bin/env python3
"""Telegram long-polling adapter for the JobOS Human Review Hub.

No webhook/public server is required. Callback buttons contain only short,
opaque single-use tokens; every decision is revalidated by Review Hub and the
canonical approval service before any state change.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.common.config import database_dsn, load_repo_env
from services.common.value_coercion import coerce_bool
from services.review.review_service_v1 import (
    ReviewError, answer_question, decide_item, review_artifacts, sync_inbox,
    safe_batch_review_items, snooze_review_item, question_quick_choices,
    document_change_summary, submit_document_feedback,
    prepare_fresh_autofill_after_human_input,
)
from services.review.approval_context_v1 import NAN, build_envelope, context_files, snapshot_context

load_repo_env()
BOT_KEY = "review_bot"


class TelegramError(RuntimeError):
    pass


def _bot_token() -> str:
    token = (os.getenv("JOBOS_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise TelegramError("Set JOBOS_TELEGRAM_BOT_TOKEN in .env.")
    return token


def _required_env() -> tuple[str, int, int]:
    token = _bot_token()
    user = (os.getenv("JOBOS_TELEGRAM_ALLOWED_USER_ID") or os.getenv("TELEGRAM_ALLOWED_USER_ID") or "").strip()
    chat = (os.getenv("JOBOS_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or user).strip()
    if not user or not chat:
        raise TelegramError("Set JOBOS_TELEGRAM_ALLOWED_USER_ID and JOBOS_TELEGRAM_CHAT_ID in .env.")
    try:
        return token, int(user), int(chat)
    except ValueError as exc:
        raise TelegramError("Telegram user/chat ids must be integers.") from exc


def api(token: str, method: str, *, data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None, timeout: int = 70) -> dict[str, Any]:
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                                 data=data, files=files, timeout=timeout)
        payload = response.json()
        if not isinstance(payload, dict):
            raise TelegramError(
                f"Telegram {method} returned {type(payload).__name__}; expected a JSON object."
            )
    except (requests.RequestException, ValueError) as exc:
        raise TelegramError(f"Telegram {method} request failed: {exc}") from exc
    if not response.ok or not coerce_bool(payload.get("ok")):
        raise TelegramError(f"Telegram {method} failed: {payload.get('description') or response.text[:300]}")
    return payload



def _safe_int(value: Any) -> int:
    try:
        if isinstance(value, bool):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _result_dict(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def _result_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]

def discover_ids(token: str) -> list[dict[str, int | str]]:
    payload = api(token, "getUpdates", data={"timeout": "0", "allowed_updates": json.dumps(["message"])}, timeout=15)
    seen: set[tuple[int, int]] = set()
    result: list[dict[str, int | str]] = []
    for update in _result_list(payload):
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        user_id, chat_id = _safe_int(sender.get("id")), _safe_int(chat.get("id"))
        if not user_id or not chat_id or (user_id, chat_id) in seen:
            continue
        seen.add((user_id, chat_id))
        result.append({"user_id": user_id, "chat_id": chat_id,
                       "username": str(sender.get("username") or ""),
                       "chat_type": str(chat.get("type") or "")})
    return result


def _callback_token(cur, item_id: str, action: str, allowed_user_id: int,
                    ttl_hours: int = 24, *, context_sha256: str | None = None,
                    payload: dict[str, Any] | None = None) -> str:
    raw = secrets.token_urlsafe(18)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    cur.execute("SELECT source_sha256,current_revision_id::text FROM human_review_items WHERE id=%s;", (item_id,))
    row = cur.fetchone()
    source_sha256 = str(row[0]) if row and len(row) > 0 and row[0] else None
    review_revision_id = str(row[1]) if row and len(row) > 1 and row[1] else None
    cur.execute(
        """INSERT INTO telegram_callback_tokens(
               review_item_id, token_sha256, action, allowed_user_id, expires_at,
               source_sha256, context_sha256, payload_json, review_revision_id)
           VALUES (%s, %s, %s, %s, now() + make_interval(hours => %s), %s, %s, %s, %s);""",
        (item_id, digest, action, allowed_user_id, ttl_hours, source_sha256, context_sha256,
         Jsonb(payload or {}), review_revision_id),
    )
    return raw


def _keyboard(cur, item_id: str, allowed_user_id: int, item_type: str,
              payload: dict[str, Any], *, context_sha256: str | None = None) -> str | None:
    def tok(action: str, payload_data: dict[str, Any] | None = None) -> str:
        return _callback_token(cur, item_id, action, allowed_user_id,
                               context_sha256=context_sha256, payload=payload_data)

    details = tok("details")
    later = tok("skip")
    open_url = str(payload.get("open_url") or "").strip()

    def render(rows: list[list[dict[str, str]]]) -> str:
        if open_url.startswith(("https://", "http://")):
            # This is deliberately secondary: Telegram opens URLs on the
            # device, not necessarily in JobOS's dedicated browser.
            rows.append([{"text": "↗ View URL on this device", "url": open_url}])
        return json.dumps({"inline_keyboard": rows}, separators=(",", ":"))

    if item_type == "question_required":
        rows = []
        for choice in question_quick_choices(cur, item_id)[:4]:
            answer_token = tok("answer", {"answer": choice, "scope": "company"})
            rows.append([{"text": f"✅ {choice}", "callback_data": f"rv:{answer_token}"}])
        other = tok("other")
        rows.append([{"text": "✏️ Other", "callback_data": f"rv:{other}"},
                     {"text": "👀 Review", "callback_data": f"rv:{details}"},
                     {"text": "⏭ Later", "callback_data": f"rv:{later}"}])
        return render(rows)

    if item_type == "sensitive_question_required":
        focus, confirm = tok("focus_browser"), tok("sensitive_confirm")
        rows = [
            [{"text": "🌐 Focus JobOS form", "callback_data": f"rv:{focus}"}],
            [{"text": "✅ Done — recheck form", "callback_data": f"rv:{confirm}"}],
            [{"text": "👀 Review", "callback_data": f"rv:{details}"},
             {"text": "⏭ Later", "callback_data": f"rv:{later}"}],
        ]
        return render(rows)

    if item_type == "autofill_review" and payload.get("execution_state") != "completed":
        revise, reject = tok("revise"), tok("reject")
        rows = [
            [{"text": "✏️ Prepare again", "callback_data": f"rv:{revise}"},
             {"text": "❌ Stop application", "callback_data": f"rv:{reject}"}],
            [{"text": "👀 Review", "callback_data": f"rv:{details}"},
             {"text": "⏭ Later", "callback_data": f"rv:{later}"}],
        ]
        return render(rows)

    if item_type == "document_review":
        feedback = tok("document_feedback")
        rows = []
        if payload.get("qa_status") == "pass":
            approve = tok("approve")
            rows.append([{"text": "✅ Approve", "callback_data": f"rv:{approve}"},
                         {"text": "✏️ Tell agent what to fix", "callback_data": f"rv:{feedback}"}])
        else:
            rows.append([{"text": "✏️ Tell agent what to fix", "callback_data": f"rv:{feedback}"}])
        rows.append([{"text": "👀 Review", "callback_data": f"rv:{details}"},
                     {"text": "⏭ Later", "callback_data": f"rv:{later}"}])
        return render(rows)

    if item_type == "reconciliation_required" and payload.get("privileged_execution_id"):
        occurred, not_occurred = tok("approve"), tok("reject")
        rows = [
            [{"text": "✅ OCCURRED", "callback_data": f"rv:{occurred}"},
             {"text": "⭕ NOT OCCURRED", "callback_data": f"rv:{not_occurred}"}],
            [{"text": "👀 Details", "callback_data": f"rv:{details}"}],
        ]
        return render(rows)

    if item_type == "reconciliation_required":
        inspected = tok("approve")
        rows = [
            [{"text": "✅ I inspected the form", "callback_data": f"rv:{inspected}"}],
            [{"text": "👀 Details", "callback_data": f"rv:{details}"},
             {"text": "⏭ Later", "callback_data": f"rv:{later}"}],
        ]
        return render(rows)

    if item_type == "action_required":
        action_kind = str(payload.get("action_kind") or "")
        if action_kind == "workflow_followup_required" and str(payload.get("expected_step") or "") == "needs_email_verification":
            focus = tok("focus_browser")
            rows = [
                [{"text": "🌐 Open verification page in JobOS", "callback_data": f"rv:{focus}"}],
                [{"text": "👀 Review", "callback_data": f"rv:{details}"},
                 {"text": "⏭ Later", "callback_data": f"rv:{later}"}],
            ]
            return render(rows)
        approve, reject = tok("approve"), tok("reject")
        label = {
            "open_apply_binding_required": "🌐 Open JobOS Apply page",
            "email_verification_binding_required": "📧 Bind OTP page",
            "email_verification_candidate_ambiguity": "📧 Confirm email",
            "workflow_followup_required": "🔄 Retry safely",
        }.get(action_kind, "✅ Continue")
        rows = [
            [{"text": label, "callback_data": f"rv:{approve}"}],
            [{"text": "👀 Review", "callback_data": f"rv:{details}"},
             {"text": "⏭ Later", "callback_data": f"rv:{later}"}],
        ]
        if action_kind.startswith("email_verification_"):
            rows[-1].append({"text": "❌ Reject email", "callback_data": f"rv:{reject}"})
        return render(rows)

    if item_type == "application_ready":
        prepare_gate, reject = tok("approve"), tok("reject")
        rows = [
            [{"text": "🔄 Retry page check", "callback_data": f"rv:{prepare_gate}"}],
            [{"text": "👀 Review", "callback_data": f"rv:{details}"},
             {"text": "⏭ Later", "callback_data": f"rv:{later}"},
             {"text": "❌ Stop", "callback_data": f"rv:{reject}"}],
        ]
        return render(rows)

    approval_type = str(payload.get("approval_type") or "")
    approve_labels = {
        "privileged_begin_application": "✅ Apply",
        "privileged_trust_external_domain": "🌐 Continue to employer site",
        "privileged_choose_create_employer_account_path": "👤 Create new account",
        "privileged_choose_navigation_target": "🌐 Use this application tab",
        "privileged_create_employer_account": "✅ Create account",
        "privileged_login_employer_account": "🔐 Login",
        "privileged_use_email_verification": "📧 Verify email",
        "privileged_accept_terms": "✅ Accept & continue",
        "privileged_upload_document": "📄 Upload document",
        "privileged_advance_application_step": "✅ Continue",
        "privileged_auth_manual_retry": "🔄 Retry auth",
        "privileged_mfa_retry": "🔄 Continue after MFA",
        "privileged_checkpoint_retry": "🔄 Continue after checkpoint",
        "privileged_submit_application": "🚨 Submit application",
        "autofill_form": "✅ Autofill",
    }
    if approval_type == "privileged_upload_document":
        document_type = str(payload.get("document_type") or "").strip().casefold()
        if document_type == "resume":
            approve_labels[approval_type] = "📄 Upload resume"
        elif document_type == "cover_letter":
            approve_labels[approval_type] = "📄 Upload cover letter"

    approve, reject = tok("approve"), tok("reject")
    if approval_type in {"privileged_login_employer_account", "privileged_auth_manual_retry", "privileged_mfa_retry", "privileged_checkpoint_retry"}:
        focus = tok("focus_browser")
        rows = [
            [{"text": "🌐 Focus JobOS page", "callback_data": f"rv:{focus}"}],
            [{"text": approve_labels[approval_type], "callback_data": f"rv:{approve}"}],
            [{"text": "👀 Review", "callback_data": f"rv:{details}"},
             {"text": "⏭ Later", "callback_data": f"rv:{later}"},
             {"text": "❌ Stop", "callback_data": f"rv:{reject}"}],
        ]
        return render(rows)

    revise = tok("revise")
    application_stop_types = {
        "privileged_begin_application", "privileged_trust_external_domain",
        "privileged_choose_create_employer_account_path", "privileged_create_employer_account",
        "privileged_accept_terms", "privileged_advance_application_step",
        "privileged_submit_application",
    }
    reject_label = "❌ Stop application" if approval_type in application_stop_types else "❌ Reject"
    rows = [
        [{"text": approve_labels.get(approval_type, "✅ Approve"), "callback_data": f"rv:{approve}"}],
        [{"text": "👀 Review", "callback_data": f"rv:{details}"},
         {"text": "✏️ Edit", "callback_data": f"rv:{revise}"},
         {"text": "⏭ Later", "callback_data": f"rv:{later}"}],
        [{"text": reject_label, "callback_data": f"rv:{reject}"}],
    ]
    return render(rows)


def _compact(value: Any, limit: int = 120) -> str:
    if value is None:
        return NAN
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    else:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _message_text(row: tuple[Any, ...], envelope: dict[str, Any] | None = None,
                  diff: dict[str, Any] | None = None) -> str:
    """Daily-use card: short, decision-first, no internal IDs."""
    from services.review.ux_policy_v1 import status_badges
    _item_id, item_type, priority, title, summary, company, role, payload = row
    payload = payload or {}
    envelope = envelope if isinstance(envelope, dict) else {}
    job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
    fit = envelope.get("fit") if isinstance(envelope.get("fit"), dict) else {}
    approval = envelope.get("approval") if isinstance(envelope.get("approval"), dict) else {}
    action_type = str(approval.get("action_type") or payload.get("approval_type") or item_type)
    risk_icon = "🔴" if priority == "urgent" and action_type == "privileged_submit_application" else ("🟡" if priority in {"urgent", "high"} else "🟢")
    lines = [f"{risk_icon} {company or 'Unknown company'} — {role or 'Application'}"]
    if not any(isinstance(envelope.get(key), dict) for key in ("job","fit","approval","browser","documents","form","auth")):
        lines.append("Context soft-fail: NaN")
    fit_score = fit.get("fit_score", job.get("fit_score"))
    if fit_score not in (None, NAN, ""):
        lines.append(f"Match: {fit_score}%")
    reviewing_doc = str(payload.get("doc_type") or "") if item_type == "document_review" else None
    lines.append(status_badges(envelope, reviewing_doc=reviewing_doc))

    if item_type == "document_review":
        changes = payload.get("resume_changes") if isinstance(payload.get("resume_changes"), list) else []
        if changes:
            lines.extend(["", "📄 Resume changes"] + [f"• {line}" for line in changes[:6]])
        else:
            lines.extend(["", str(title)])
    elif item_type == "question_required":
        lines.extend(["", f"❓ {payload.get('question') or title}"])
        if payload.get("missing_information"):
            lines.append(str(payload.get("missing_information")))
    elif item_type == "sensitive_question_required":
        # Keep legal/immigration answers out of Telegram.  The card exposes
        # only the exact employer question and focuses the dedicated JobOS
        # browser so the candidate can attest there.
        lines.extend(["", f"⚖️ {payload.get('question') or title}"])
        lines.append("Answer this exact employer wording manually in the focused JobOS form.")
    elif item_type == "reconciliation_required":
        lines.extend(["", "⚠️ Browser outcome is uncertain.", "JobOS will not replay it automatically."])
    else:
        human_summary = str(summary or title or "Action required")
        if len(human_summary) > 420:
            human_summary = human_summary[:419] + "…"
        lines.extend(["", human_summary])

    if action_type == "privileged_submit_application":
        lines.extend(["", "🚨 Final irreversible Submit — separate approval required."])
    elif action_type in {"privileged_login_employer_account", "privileged_auth_manual_retry"}:
        lines.extend(["", "🔐 Login is required. Use the button; no terminal command is needed."])
    elif action_type in {"privileged_mfa_retry", "privileged_checkpoint_retry"}:
        lines.extend(["", "Complete the browser checkpoint, then tap Continue."])

    if diff and not diff.get("baseline") and diff.get("changed"):
        lines.append(f"\nChanged since last card: {len(diff.get('changed') or [])} item(s). Tap 👀 Review.")
    return "\n".join(lines)[:1400]


def _detail_text(row: tuple[Any, ...], envelope: dict[str, Any] | None = None,
                 diff: dict[str, Any] | None = None) -> str:
    item_id, item_type, priority, title, summary, company, role, payload = row
    payload = payload or {}
    envelope = envelope if isinstance(envelope, dict) else {}
    job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
    approval = envelope.get("approval") if isinstance(envelope.get("approval"), dict) else {}
    browser = envelope.get("browser") if isinstance(envelope.get("browser"), dict) else {}
    fit = envelope.get("fit") if isinstance(envelope.get("fit"), dict) else {}
    documents = envelope.get("documents") if isinstance(envelope.get("documents"), dict) else {}
    form = envelope.get("form") if isinstance(envelope.get("form"), dict) else {}
    auth = envelope.get("auth") if isinstance(envelope.get("auth"), dict) else {}
    action_type = approval.get("action_type") or payload.get("approval_type") or item_type
    lines = ["🧭 JobOS Human Approval", f"{company or NAN} — {role or NAN}", f"Action: {action_type}", "", str(title)]
    if summary:
        lines.extend(["", str(summary)])
    lines.extend([
        "", "JOB",
        f"Location: {_compact(job.get('location', NAN))} | Work mode: {_compact(job.get('work_mode', NAN))}",
        f"Fit: {_compact(fit.get('fit_score', job.get('fit_score', NAN)))} / {_compact(fit.get('fit_decision', job.get('fit_decision', NAN)))}",
        f"Fit reason: {_compact(fit.get('decision_reason', NAN), 260)}",
        f"Matched: {_compact(fit.get('matched_requirements', NAN), 260)}",
        f"Missing/weak: {_compact(fit.get('missing_or_weak_requirements', NAN), 260)}",
        f"Hard blockers: {_compact(fit.get('hard_blockers', NAN), 180)}",
        f"JD: {_compact(job.get('job_url', NAN), 180)}",
        "", "BROWSER",
        f"URL: {_compact(browser.get('target_url', auth.get('current_url', NAN)), 180)}",
        f"Auth state: {_compact(auth.get('state', NAN))} | Platform: {_compact(auth.get('platform_hint', NAN))}",
    ])
    resume = documents.get("resume") if isinstance(documents, dict) else NAN
    cover = documents.get("cover_letter") if isinstance(documents, dict) else NAN
    if isinstance(resume, dict):
        resume_text = f"{resume.get('filename', NAN)} [{str(resume.get('sha256', NAN))[:12]}]"
    else:
        resume_text = NAN
    if isinstance(cover, dict):
        cover_text = f"{cover.get('filename', NAN)} [{str(cover.get('sha256', NAN))[:12]}]"
    else:
        cover_text = NAN
    lines.extend(["", "DOCUMENTS", f"Resume: {resume_text}", f"Cover letter: {cover_text}"])
    proposed = form.get("proposed_fields", NAN) if isinstance(form, dict) else NAN
    paused = form.get("paused_fields", NAN) if isinstance(form, dict) else NAN
    blockers = form.get("required_blockers", NAN) if isinstance(form, dict) else NAN
    lines.extend(["", "FORM", f"Fields: {_compact(proposed, 500)}", f"Paused: {_compact(paused, 240)}", f"Required blockers: {_compact(blockers, 240)}"])
    if diff:
        changes = diff.get("changed") if isinstance(diff.get("changed"), list) else []
        lines.extend(["", "DIFF VS PREVIOUS APPROVAL MESSAGE"])
        if diff.get("baseline"):
            lines.append("Baseline: no previous same application/action package.")
        elif not changes:
            lines.append("No material context changes.")
        else:
            for change in changes[:10]:
                lines.append(f"• {change.get('path')}: {_compact(change.get('before'), 55)} → {_compact(change.get('after'), 55)}")
            if len(changes) > 10:
                lines.append(f"• … {len(changes) - 10} more change(s) in attached context JSON")
    if item_type == "question_required":
        lines.extend(["", f"Question: {payload.get('question') or NAN}", "Tap ✏️ Other, then reply naturally in this chat."])
    elif item_type == "reconciliation_required":
        if payload.get("privileged_execution_id"):
            lines.extend(["", "⚠️ Browser effect is uncertain. Choose OCCURRED or NOT OCCURRED. This consumed approval will never be replayed."])
        else:
            lines.extend(["", "⚠️ Inspect the form in the JobOS browser first. Then tap ‘I inspected the form’; JobOS retires the old capability and requires a fresh approval for any later write."])
    elif item_type == "application_ready":
        lines.extend(["", "JobOS could not classify the current page automatically. Retry only refreshes the page binding; Submit still requires its own explicit confirmation."])
    lines.extend(["", "Context delivery is soft-fail: missing sections show NaN and do not remove approval controls.",
                  f"Priority: {priority}"])
    return "\n".join(lines)[:3900]


def _delivery_dedupe(*, item_id: str, chat_id: int, kind: str, method: str,
                     context_sha256: str | None = None, artifact_sha256: str | None = None,
                     force_nonce: str | None = None) -> str:
    raw = {"item_id": item_id, "chat_id": chat_id, "kind": kind, "method": method,
           "context_sha256": context_sha256, "artifact_sha256": artifact_sha256,
           "force_nonce": force_nonce}
    return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _redact_telegram_outbox_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return an audit-safe payload that never persists Telegram capabilities.

    ``callback_data`` contains single-use bearer tokens. The actual request is
    hashed separately, while the durable JSON keeps only a digest of each
    callback value so operators can compare requests without recovering a live
    capability from PostgreSQL.
    """
    def scrub(value: Any, *, key: str | None = None) -> Any:
        if key == "callback_data" and isinstance(value, str):
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            return {"redacted": "callback_data", "sha256": digest}
        if isinstance(value, dict):
            return {str(k): scrub(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    safe = dict(payload)
    markup = safe.get("reply_markup")
    if isinstance(markup, str):
        try:
            parsed = json.loads(markup)
        except Exception:
            # Fail closed: an opaque markup string could contain callback data.
            safe["reply_markup"] = {
                "redacted": "opaque_reply_markup",
                "sha256": hashlib.sha256(markup.encode("utf-8")).hexdigest(),
            }
        else:
            safe["reply_markup"] = scrub(parsed)
    elif markup is not None:
        safe["reply_markup"] = scrub(markup)
    return scrub(safe)


def _prepare_delivery(cur, *, item_id: str, chat_id: int, kind: str, method: str,
                      payload: dict[str, Any], artifact_sha256: str | None = None,
                      context_sha256: str | None = None, force_nonce: str | None = None) -> str | None:
    """Persist the Telegram send intent before network I/O.

    A pre-existing dedupe row is never blindly resent: ``sending``/``uncertain``
    means Telegram may already have accepted it.
    """
    dedupe = _delivery_dedupe(item_id=item_id, chat_id=chat_id, kind=kind, method=method,
                              context_sha256=context_sha256, artifact_sha256=artifact_sha256,
                              force_nonce=force_nonce)
    request_sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    stored_payload = _redact_telegram_outbox_payload(payload)
    cur.execute(
        """INSERT INTO telegram_review_deliveries(
               review_item_id,chat_id,message_id,delivery_kind,status,error_message,
               artifact_sha256,context_sha256,dedupe_key,method,payload_json,request_sha256,
               payload_redaction_version,attempt_count,lease_expires_at,delivered_at,updated_at)
           VALUES (%s,%s,NULL,%s,'sending',NULL,%s,%s,%s,%s,%s,%s,1,1,now()+interval '5 minutes',NULL,now())
           ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
           RETURNING id::text;""",
        (item_id, chat_id, kind, artifact_sha256, context_sha256, dedupe, method,
         Jsonb(stored_payload), request_sha),
    )
    row = cur.fetchone()
    if not row:
        return None
    delivery_id = str(row[0])
    cur.execute(
        """INSERT INTO telegram_delivery_attempts(delivery_id,attempt_no,status)
           VALUES (%s,1,'started');""", (delivery_id,),
    )
    # Correctness boundary: intent/attempt must survive a process death during api().
    cur.connection.commit()
    return delivery_id


def _reap_stale_transport(cur) -> None:
    """Turn ambiguous expired transport leases into reconciliation state.

    Telegram cannot participate in our PostgreSQL transaction. A dead sender or
    update handler therefore leaves an ambiguity boundary, not a retryable row.
    """
    cur.execute(
        """WITH stale AS (
               UPDATE telegram_review_deliveries
                  SET status='uncertain',lease_expires_at=NULL,updated_at=now(),
                      error_message=coalesce(error_message,'Telegram send lease expired; delivery may have occurred. Do not resend blindly.')
                WHERE status='sending' AND lease_expires_at IS NOT NULL AND lease_expires_at <= now()
                RETURNING id
           )
           UPDATE telegram_delivery_attempts a
              SET status='uncertain',finished_at=coalesce(finished_at,now()),
                  error_message=coalesce(error_message,'Telegram send lease expired before local acknowledgement.')
             FROM stale s WHERE a.delivery_id=s.id AND a.status='started';"""
    )
    cur.execute(
        """UPDATE telegram_updates
              SET status='uncertain',lease_expires_at=NULL,
                  error_message=coalesce(error_message,'Telegram update processing lease expired; local effects may have occurred.')
            WHERE status='processing' AND lease_expires_at IS NOT NULL AND lease_expires_at <= now();"""
    )


def _finish_delivery(cur, delivery_id: str, message_id: int, *, commit: bool = True) -> None:
    cur.execute(
        """UPDATE telegram_review_deliveries
              SET status='sent',message_id=%s,delivered_at=now(),updated_at=now(),lease_expires_at=NULL
            WHERE id=%s AND status='sending';""", (message_id, delivery_id),
    )
    cur.execute(
        """UPDATE telegram_delivery_attempts
              SET status='sent',telegram_message_id=%s,finished_at=now()
            WHERE delivery_id=%s AND attempt_no=1 AND status='started';""", (message_id, delivery_id),
    )
    if commit:
        cur.connection.commit()


def _uncertain_delivery(cur, delivery_id: str, exc: BaseException) -> None:
    error = str(exc)[:1000]
    cur.execute(
        """UPDATE telegram_review_deliveries
              SET status='uncertain',error_message=%s,updated_at=now(),lease_expires_at=NULL
            WHERE id=%s AND status='sending';""", (error, delivery_id),
    )
    cur.execute(
        """UPDATE telegram_delivery_attempts SET status='uncertain',error_message=%s,finished_at=now()
            WHERE delivery_id=%s AND attempt_no=1 AND status='started';""", (error, delivery_id),
    )
    cur.connection.commit()


def _send_bound_force_reply_prompt(
    conn, token: str, *, item_id: str, chat_id: int, source_sha256: str,
    callback_token_id: str, prompt_kind: str, text: str, placeholder: str,
) -> int:
    """Persist exact reply binding + transport intent before Telegram I/O."""
    if prompt_kind not in {"document_feedback", "question"}:
        raise ReviewError(f"Unsupported ForceReply prompt kind: {prompt_kind}")
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "reply_markup": json.dumps({"force_reply": True, "input_field_placeholder": placeholder}),
    }
    with conn.cursor() as cur:
        if prompt_kind == "document_feedback":
            cur.execute(
                """INSERT INTO telegram_control_surface_state(
                       chat_id,pending_document_review_item_id,pending_document_source_sha256,
                       pending_document_feedback_expires_at,pending_document_prompt_message_id,updated_at)
                   VALUES (%s,%s,%s,now()+interval '15 minutes',NULL,now())
                   ON CONFLICT (chat_id) DO UPDATE SET
                     pending_document_review_item_id=EXCLUDED.pending_document_review_item_id,
                     pending_document_source_sha256=EXCLUDED.pending_document_source_sha256,
                     pending_document_feedback_expires_at=EXCLUDED.pending_document_feedback_expires_at,
                     pending_document_prompt_message_id=NULL,updated_at=now();""",
                (chat_id, item_id, source_sha256),
            )
            delivery_kind = "document_feedback_prompt"
        else:
            cur.execute(
                """INSERT INTO telegram_control_surface_state(
                       chat_id,pending_question_review_item_id,pending_question_source_sha256,
                       pending_question_expires_at,pending_question_prompt_message_id,updated_at)
                   VALUES (%s,%s,%s,now()+interval '15 minutes',NULL,now())
                   ON CONFLICT (chat_id) DO UPDATE SET
                     pending_question_review_item_id=EXCLUDED.pending_question_review_item_id,
                     pending_question_source_sha256=EXCLUDED.pending_question_source_sha256,
                     pending_question_expires_at=EXCLUDED.pending_question_expires_at,
                     pending_question_prompt_message_id=NULL,updated_at=now();""",
                (chat_id, item_id, source_sha256),
            )
            delivery_kind = "question_reply_prompt"
        # Consume the callback in the same durable pre-network transaction.
        cur.execute("UPDATE telegram_callback_tokens SET used_at=now() WHERE id=%s AND used_at IS NULL;", (callback_token_id,))
        delivery_id = _prepare_delivery(
            cur, item_id=item_id, chat_id=chat_id, kind=delivery_kind,
            method="sendMessage", payload=payload, context_sha256=source_sha256,
            force_nonce=callback_token_id,
        )
        if not delivery_id:
            dedupe = _delivery_dedupe(
                item_id=item_id, chat_id=chat_id, kind=delivery_kind, method="sendMessage",
                context_sha256=source_sha256, force_nonce=callback_token_id,
            )
            cur.execute(
                """SELECT status,message_id FROM telegram_review_deliveries
                    WHERE dedupe_key=%s;""", (dedupe,),
            )
            prior = cur.fetchone()
            if prior and str(prior[0]) == "sent" and prior[1]:
                message_id = int(prior[1])
                if prompt_kind == "document_feedback":
                    cur.execute("UPDATE telegram_control_surface_state SET pending_document_prompt_message_id=%s,updated_at=now() WHERE chat_id=%s;", (message_id, chat_id))
                else:
                    cur.execute("UPDATE telegram_control_surface_state SET pending_question_prompt_message_id=%s,updated_at=now() WHERE chat_id=%s;", (message_id, chat_id))
                conn.commit()
                return message_id
            conn.commit()
            raise ReviewError("That Telegram prompt delivery is already in-flight/uncertain; refresh instead of resending it.")

    # _prepare_delivery committed the pending state, callback consumption and
    # send intent, releasing DB locks before the external Telegram call.
    try:
        sent = api(token, "sendMessage", data=payload)
        message_id = int(sent["result"]["message_id"])
        with conn.cursor() as cur:
            # Acknowledgement and reply-binding become durable atomically after
            # the external send. If that commit fails, the pre-send outbox row
            # remains ambiguous and is reconciled as ``uncertain``; a reply is
            # never accepted against an unbound prompt id.
            _finish_delivery(cur, delivery_id, message_id, commit=False)
            if prompt_kind == "document_feedback":
                cur.execute("UPDATE telegram_control_surface_state SET pending_document_prompt_message_id=%s,updated_at=now() WHERE chat_id=%s;", (message_id, chat_id))
            else:
                cur.execute("UPDATE telegram_control_surface_state SET pending_question_prompt_message_id=%s,updated_at=now() WHERE chat_id=%s;", (message_id, chat_id))
        conn.commit()
        return message_id
    except Exception as exc:
        conn.rollback()
        with conn.cursor() as cur:
            _uncertain_delivery(cur, delivery_id, exc)
        raise


def _deliver_artifact(cur, token: str, *, item_id: str, chat_id: int,
                      artifact: dict[str, Any]) -> bool:
    """Best-effort artifact delivery with a durable pre-send outbox row."""
    path = Path(artifact["file_path"]).expanduser()
    if not path.is_file():
        return False
    method = "sendPhoto" if artifact["mime_type"].startswith("image/") else "sendDocument"
    field = "photo" if method == "sendPhoto" else "document"
    data = {"chat_id": str(chat_id), "caption": artifact["filename"]}
    delivery_id = _prepare_delivery(
        cur, item_id=item_id, chat_id=chat_id, kind="artifact", method=method, payload=data,
        artifact_sha256=artifact["sha256"],
    )
    if not delivery_id:
        return False
    try:
        with path.open("rb") as stream:
            sent = api(token, method, data=data,
                       files={field: (artifact["filename"], stream, artifact["mime_type"])})
        _finish_delivery(cur, delivery_id, int(sent["result"]["message_id"]))
        return True
    except Exception as exc:
        _uncertain_delivery(cur, delivery_id, exc)
        return False


def _deliver_memory_artifact(cur, token: str, *, item_id: str, chat_id: int,
                             filename: str, payload: bytes, mime_type: str) -> bool:
    digest = hashlib.sha256(payload).hexdigest()
    data = {"chat_id": str(chat_id), "caption": filename}
    delivery_id = _prepare_delivery(cur, item_id=item_id, chat_id=chat_id, kind="artifact",
                                    method="sendDocument", payload=data, artifact_sha256=digest)
    if not delivery_id:
        return False
    try:
        sent = api(token, "sendDocument", data=data,
                   files={"document": (filename, io.BytesIO(payload), mime_type)})
        _finish_delivery(cur, delivery_id, int(sent["result"]["message_id"]))
        return True
    except Exception as exc:
        _uncertain_delivery(cur, delivery_id, exc)
        return False

def _ui_token(cur, action: str, allowed_user_id: int, *, payload: dict[str, Any] | None = None,
              ttl_minutes: int = 30) -> str:
    raw = secrets.token_urlsafe(18)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    cur.execute(
        """INSERT INTO telegram_ui_tokens(token_sha256, action, allowed_user_id, payload_json, expires_at)
           VALUES (%s,%s,%s,%s,now() + make_interval(mins => %s));""",
        (digest, action, allowed_user_id, Jsonb(payload or {}), ttl_minutes),
    )
    return raw


def _dashboard_data(cur) -> dict[str, Any]:
    sync_inbox(cur)
    cur.execute(
        """SELECT count(*),
                  count(*) FILTER (WHERE priority IN ('urgent','high')),
                  count(*) FILTER (WHERE item_type='reconciliation_required')
             FROM v_human_review_inbox;"""
    )
    pending, needs_attention, reconciliation = cur.fetchone()
    cur.execute(
        """SELECT current_step, count(*)
             FROM applications
            WHERE status NOT IN ('submitted','abandoned')
              AND current_step NOT IN ('filtered_out','fit_rejected')
            GROUP BY current_step;"""
    )
    by_step = {str(step): int(count) for step, count in cur.fetchall()}
    active = sum(by_step.values())
    waiting_user = sum(by_step.get(step, 0) for step in (
        'docs_verified','needs_account_auth','needs_email_verification','needs_mfa',
        'needs_human_checkpoint','application_ready','awaiting_approval','needs_reconciliation'
    ))
    safe = safe_batch_review_items(cur, limit=50)
    cur.execute(
        """SELECT count(*),
                  count(*) FILTER (WHERE current_step='filtered_out'),
                  count(*) FILTER (WHERE current_step IN ('docs_verified','application_form_ready','awaiting_approval','application_ready'))
             FROM applications
            WHERE created_at >= now() - interval '12 hours';"""
    )
    recent_found, recent_filtered, recent_ready = cur.fetchone()
    return {
        "active": active,
        "pending": int(pending or 0),
        "attention": int(needs_attention or 0),
        "reconciliation": int(reconciliation or 0),
        "waiting_user": int(waiting_user),
        "safe": safe,
        "by_step": by_step,
        "recent_found": int(recent_found or 0),
        "recent_filtered": int(recent_filtered or 0),
        "recent_ready": int(recent_ready or 0),
    }


def _dashboard_text(data: dict[str, Any]) -> str:
    safe_count = len(data.get("safe") or [])
    red = int(data.get("reconciliation") or 0)
    lines = [
        "🤖 JOBOS",
        "",
        f"Recent 12h: {int(data.get('recent_found') or 0)} found · {int(data.get('recent_filtered') or 0)} low-fit/filtered · {int(data.get('recent_ready') or 0)} ready-stage",
        "",
        f"🟢 {int(data.get('active') or 0)} jobs đang xử lý",
        f"🟡 {int(data.get('pending') or 0)} việc cần bạn",
        f"🔴 {red} việc cần recovery" if red else "🔴 0 việc bị kẹt",
    ]
    if safe_count:
        apps = len({row['application_id'] for row in data['safe']})
        lines.extend(["", f"✅ {safe_count} low-risk decision(s) trên {apps} application(s) có thể approve cùng lúc."])
    if not data.get("pending"):
        lines.extend(["", "Không có việc nào cần thao tác. JobOS sẽ tiếp tục tự động những bước an toàn."])
    return "\n".join(lines)


def dispatch_dashboard(conn, token: str, allowed_user_id: int, chat_id: int, *, force: bool = False) -> int:
    with conn.cursor() as cur:
        data = _dashboard_data(cur)
        safe = data.get("safe") or []
        batch_payload = {"items": [
            {"item_id": row["item_id"], "application_id": row["application_id"],
             "source_sha256": row.get("source_sha256") or ""}
            for row in safe
        ]}
        text = _dashboard_text(data)
        digest = hashlib.sha256((text + json.dumps(batch_payload, sort_keys=True)).encode("utf-8")).hexdigest()
        cur.execute(
            """SELECT dashboard_message_id, last_digest,
                      updated_at > now() - interval '20 minutes'
                 FROM telegram_control_surface_state WHERE chat_id=%s;""",
            (chat_id,),
        )
        existing = cur.fetchone()
        # Dashboard buttons expire after 30 minutes.  Re-render before that
        # point even if the textual state did not change, otherwise its own
        # Refresh button becomes a dead end.
        if existing and existing[1] == digest and bool(existing[2]) and not force:
            conn.commit()
            return 0
        rows = []
        if safe:
            approve = _ui_token(cur, "approve_safe", allowed_user_id, payload=batch_payload)
            rows.append([{"text": f"✅ Approve {len(safe)} safe", "callback_data": f"ux:{approve}"}])
        if data.get("pending"):
            review = _ui_token(cur, "review_next", allowed_user_id)
            rows.append([{"text": f"🟡 Review next ({data['pending']})", "callback_data": f"ux:{review}"}])
        refresh = _ui_token(cur, "refresh", allowed_user_id)
        rows.append([{"text": "🔄 Refresh", "callback_data": f"ux:{refresh}"}])
        keyboard = json.dumps({"inline_keyboard": rows}, separators=(",", ":"))
        conn.commit()  # callback tokens exist before Telegram receives them
        message_id = int(existing[0]) if existing and existing[0] else None
        if message_id:
            try:
                api(token, "editMessageText", data={"chat_id": str(chat_id), "message_id": str(message_id),
                                                     "text": text, "reply_markup": keyboard}, timeout=20)
            except TelegramError:
                message_id = None
        if not message_id:
            sent = api(token, "sendMessage", data={"chat_id": str(chat_id), "text": text, "reply_markup": keyboard})
            message_id = int(sent["result"]["message_id"])
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO telegram_control_surface_state(chat_id,dashboard_message_id,last_digest,updated_at)
                   VALUES (%s,%s,%s,now())
                   ON CONFLICT (chat_id) DO UPDATE
                   SET dashboard_message_id=EXCLUDED.dashboard_message_id,
                       last_digest=EXCLUDED.last_digest, updated_at=now();""",
                (chat_id, message_id, digest),
            )
        conn.commit()
        return 1


def _load_exact_context(cur, item_id: str, context_sha: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if context_sha:
        cur.execute(
            """SELECT context_json, diff_json FROM approval_context_snapshots
                 WHERE review_item_id=%s AND context_sha256=%s
                 ORDER BY created_at DESC LIMIT 1;""", (item_id, context_sha)
        )
        row = cur.fetchone()
        if row:
            return dict(row[0] or {}), dict(row[1] or {})
    cur.execute("SELECT application_id::text FROM human_review_items WHERE id=%s;", (item_id,))
    row = cur.fetchone()
    if not row:
        raise ReviewError("Review item not found.")
    envelope = build_envelope(cur, item_id, row[0])
    return envelope, {"baseline": True, "changed": []}


def _send_review_details(conn, token: str, *, item_id: str, chat_id: int, context_sha: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT h.id::text,h.item_type,h.priority,h.title,h.summary_text,a.company,a.job_title,h.payload_json,
                      h.application_id::text
                 FROM human_review_items h JOIN applications a ON a.id=h.application_id
                WHERE h.id=%s;""", (item_id,)
        )
        raw = cur.fetchone()
        if not raw:
            raise ReviewError("Review item not found.")
        row, application_id = raw[:8], raw[8]
        envelope, diff = _load_exact_context(cur, item_id, context_sha)
        text = _detail_text(row, envelope, diff)
        changes = document_change_summary(cur, item_id)
        if changes:
            text = (text + "\n\nVERIFIED RESUME CHANGES\n" + "\n".join(f"• {line}" for line in changes))[:3900]
        api(token, "sendMessage", data={"chat_id": str(chat_id), "text": text})
        for artifact in review_artifacts(cur, item_id):
            _deliver_artifact(cur, token, item_id=item_id, chat_id=chat_id, artifact=artifact)
            conn.commit()
        for extra in context_files(envelope):
            _deliver_artifact(cur, token, item_id=item_id, chat_id=chat_id,
                              artifact={"file_path": extra["path"], "filename": extra["filename"],
                                        "mime_type": extra["mime_type"], "sha256": extra["sha256"]})
            conn.commit()
        job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
        jd_text = job.get("jd_text") if isinstance(job, dict) else None
        if isinstance(jd_text, str) and jd_text != NAN and jd_text.strip():
            _deliver_memory_artifact(cur, token, item_id=item_id, chat_id=chat_id,
                                     filename=f"{application_id}-job-description.txt",
                                     payload=jd_text.encode("utf-8"), mime_type="text/plain")
        _deliver_memory_artifact(cur, token, item_id=item_id, chat_id=chat_id,
                                 filename=f"{application_id}-approval-context.json",
                                 payload=json.dumps({"context": envelope, "diff": diff}, ensure_ascii=False,
                                                    indent=2, default=str).encode("utf-8"),
                                 mime_type="application/json")
        conn.commit()


def _approve_safe_batch(conn, *, items: list[dict[str, Any]], actor: str) -> list[dict[str, Any]]:
    from services.review.ux_policy_v1 import is_batch_safe_item
    wanted = sorted(items, key=lambda item: (str(item.get("application_id") or ""), str(item.get("item_id") or "")))
    if not wanted:
        raise ReviewError("This safe batch is empty or stale.")
    results = []
    with conn.cursor() as cur:
        for expected in wanted:
            cur.execute(
                """SELECT application_id::text,item_type,status,source_sha256,payload_json,
                          snoozed_until IS NULL OR snoozed_until <= now()
                     FROM human_review_items WHERE id=%s;""", (expected.get("item_id"),)
            )
            row = cur.fetchone()
            if not row or row[2] not in {"pending", "needs_revision"} or not bool(row[5]):
                raise ReviewError("Safe batch changed; refresh the inbox before approving.")
            if str(row[0]) != str(expected.get("application_id") or ""):
                raise ReviewError("Safe batch application binding changed.")
            if str(row[3] or "") != str(expected.get("source_sha256") or ""):
                raise ReviewError("Safe batch content changed; refresh before approving.")
            if not is_batch_safe_item(item_type=str(row[1]), payload=dict(row[4] or {})):
                raise ReviewError("A review item is no longer batch-safe; review it individually.")
        # All exact identities were checked in one transaction. decide_item()
        # re-checks every underlying capability before changing it.
        for expected in wanted:
            results.append(decide_item(conn, str(expected["item_id"]), decision="approve", actor=actor,
                                       note="One-tap safe batch approval"))
    return results


def _handle_ui_callback(conn, token: str, allowed_user_id: int, callback: dict[str, Any]) -> None:
    callback_id = str(callback.get("id") or "")
    sender_id = int((callback.get("from") or {}).get("id") or 0)
    chat_id = int((callback.get("message") or {}).get("chat", {}).get("id") or 0)
    data = str(callback.get("data") or "")
    if sender_id != allowed_user_id:
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Not authorized", "show_alert": "true"})
        return
    digest = hashlib.sha256(data[3:].encode("utf-8")).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id::text,action,allowed_user_id,payload_json,expires_at,used_at
                 FROM telegram_ui_tokens WHERE token_sha256=%s FOR UPDATE;""", (digest,)
        )
        row = cur.fetchone()
        if not row or row[2] != sender_id or row[5] is not None:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Button expired; refresh JobOS", "show_alert": "true"})
            if chat_id:
                dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
            return
        cur.execute("SELECT %s < now();", (row[4],))
        if cur.fetchone()[0]:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Button expired; refresh JobOS", "show_alert": "true"})
            if chat_id:
                dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
            return
        token_id, action, payload = row[0], str(row[1]), dict(row[3] or {})
    try:
        if action == "approve_safe":
            results = _approve_safe_batch(conn, items=list(payload.get("items") or []), actor=f"telegram:{sender_id}")
            with conn.cursor() as cur:
                cur.execute("UPDATE telegram_ui_tokens SET used_at=now() WHERE id=%s;", (token_id,))
                sync_inbox(cur)
            conn.commit()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": f"Approved {len(results)} safe item(s)"})
            if chat_id:
                api(token, "sendMessage", data={"chat_id": str(chat_id), "text": f"🚀 {len(results)} safe decision(s) approved. JobOS will continue automatically; irreversible Submit remains separate."})
                dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
            return
        with conn.cursor() as cur:
            cur.execute("UPDATE telegram_ui_tokens SET used_at=now() WHERE id=%s;", (token_id,))
        conn.commit()
        if action == "review_next":
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Opening next review"})
            dispatch_pending(conn, token, allowed_user_id, chat_id, limit=1, force=True)
        else:
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Refreshed"})
            dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
    except ReviewError as exc:
        conn.rollback()
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": str(exc)[:180], "show_alert": "true"})


def dispatch_pending(conn, token: str, allowed_user_id: int, chat_id: int, *, limit: int = 20,
                     force: bool = False, urgent_only: bool = False) -> int:
    """Deliver compact actionable cards; full artifacts are progressive disclosure."""
    with conn.cursor() as cur:
        _reap_stale_transport(cur)
        sync_inbox(cur)
        conn.commit()
        where = "WHERE v.priority='urgent'" if urgent_only else ""
        cur.execute(
            f"""SELECT v.review_item_id::text, v.item_type, v.priority, v.title,
                       v.summary_text, v.company, v.job_title, v.payload_json,
                       v.application_id::text
                  FROM v_human_review_inbox v
                  {where}
                 LIMIT %s;""",
            (max(1, min(int(limit), 50)),),
        )
        rows = cur.fetchall()
        delivered = 0
        for raw in rows:
            raw_row, application_id = raw[:8], raw[8]
            row_payload = dict(raw_row[7] or {})
            if urgent_only:
                from services.review.ux_policy_v1 import is_batch_safe_item
                if is_batch_safe_item(item_type=str(raw_row[1]), payload=row_payload):
                    continue
            if raw_row[1] == "document_review":
                changes = document_change_summary(cur, raw_row[0])
                if changes:
                    row_payload["resume_changes"] = changes
            row = tuple(list(raw_row[:7]) + [row_payload])
            item_id = row[0]
            try:
                envelope = build_envelope(cur, item_id, application_id)
            except Exception:
                envelope = {"schema": "jobos-human-approval-envelope-v1", "job": NAN, "fit": NAN,
                            "approval": NAN, "browser": NAN, "documents": NAN, "form": NAN, "auth": NAN}
            approval = envelope.get("approval") if isinstance(envelope.get("approval"), dict) else {}
            browser = envelope.get("browser") if isinstance(envelope.get("browser"), dict) else {}
            auth = envelope.get("auth") if isinstance(envelope.get("auth"), dict) else {}
            job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
            open_url = str(browser.get("target_url") or auth.get("current_url") or job.get("job_url") or "").strip()
            if open_url.startswith(("https://", "http://")):
                row_payload["open_url"] = open_url
                row = tuple(list(raw_row[:7]) + [row_payload])
            action_scope = str(approval.get("action_type") or row[1])
            try:
                context_sha, diff = snapshot_context(cur, review_item_id=item_id, application_id=application_id,
                                                     action_scope=action_scope, envelope=envelope)
            except Exception:
                context_sha = hashlib.sha256(json.dumps(envelope, sort_keys=True, default=str).encode()).hexdigest()
                diff = {"baseline": True, "changed": []}
            cur.execute("""SELECT 1 FROM telegram_review_deliveries
                            WHERE review_item_id=%s AND chat_id=%s AND delivery_kind='summary'
                              AND context_sha256=%s AND status='sent' LIMIT 1;""",
                        (item_id, chat_id, context_sha))
            summary_sent = bool(cur.fetchone())
            cur.execute("""SELECT 1 FROM telegram_callback_tokens
                            WHERE review_item_id=%s AND allowed_user_id=%s
                              AND used_at IS NULL AND expires_at > now() LIMIT 1;""",
                        (item_id, allowed_user_id))
            live_callback = bool(cur.fetchone())
            if not force and summary_sent and live_callback:
                continue

            keyboard = _keyboard(cur, item_id, allowed_user_id, row[1], row_payload,
                                 context_sha256=context_sha)
            conn.commit()
            data: dict[str, Any] = {"chat_id": str(chat_id), "text": _message_text(row, envelope, diff)}
            if keyboard:
                data["reply_markup"] = keyboard
            delivery_id = _prepare_delivery(
                cur, item_id=item_id, chat_id=chat_id, kind="summary", method="sendMessage",
                payload=data, context_sha256=context_sha,
                force_nonce=(secrets.token_hex(8) if force else None),
            )
            if not delivery_id:
                continue
            try:
                sent = api(token, "sendMessage", data=data)
                _finish_delivery(cur, delivery_id, int(sent["result"]["message_id"]))
                delivered += 1
            except Exception as exc:
                _uncertain_delivery(cur, delivery_id, exc)
                # Do not blind-replay an ambiguous Telegram send. The durable
                # outbox row is intentionally left for operator reconciliation.
                continue
        return delivered


def _load_offset(cur) -> int:
    cur.execute("SELECT update_offset FROM telegram_bot_state WHERE bot_key = %s;", (BOT_KEY,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _save_offset(cur, offset: int) -> None:
    cur.execute(
        """INSERT INTO telegram_bot_state(bot_key, update_offset, updated_at)
           VALUES (%s, %s, now())
           ON CONFLICT (bot_key) DO UPDATE SET update_offset = EXCLUDED.update_offset, updated_at = now();""",
        (BOT_KEY, offset),
    )


def _clear_pending_question(cur, chat_id: int, *, item_id: str | None = None) -> None:
    """Remove a natural-reply capture after any alternate terminal action."""
    if item_id:
        cur.execute(
            """UPDATE telegram_control_surface_state
                  SET pending_question_review_item_id=NULL,
                      pending_question_source_sha256=NULL,
                      pending_question_expires_at=NULL,
                      pending_question_prompt_message_id=NULL,
                      updated_at=now()
                WHERE chat_id=%s AND pending_question_review_item_id=%s;""",
            (chat_id, item_id),
        )
    else:
        cur.execute(
            """UPDATE telegram_control_surface_state
                  SET pending_question_review_item_id=NULL,
                      pending_question_source_sha256=NULL,
                      pending_question_expires_at=NULL,
                      pending_question_prompt_message_id=NULL,
                      updated_at=now() WHERE chat_id=%s;""",
            (chat_id,),
        )


def _clear_pending_document_feedback(cur, chat_id: int, *, item_id: str | None = None) -> None:
    """Clear an exact, short-lived document-agent feedback capture."""
    if item_id:
        cur.execute(
            """UPDATE telegram_control_surface_state
                  SET pending_document_review_item_id=NULL,
                      pending_document_source_sha256=NULL,
                      pending_document_feedback_expires_at=NULL,
                      pending_document_prompt_message_id=NULL,
                      updated_at=now()
                WHERE chat_id=%s AND pending_document_review_item_id=%s;""",
            (chat_id, item_id),
        )
    else:
        cur.execute(
            """UPDATE telegram_control_surface_state
                  SET pending_document_review_item_id=NULL,
                      pending_document_source_sha256=NULL,
                      pending_document_feedback_expires_at=NULL,
                      pending_document_prompt_message_id=NULL,
                      updated_at=now() WHERE chat_id=%s;""",
            (chat_id,),
        )


def handle_callback(conn, token: str, allowed_user_id: int, callback: dict[str, Any]) -> None:
    callback_id = str(callback.get("id") or "")
    sender_id = int((callback.get("from") or {}).get("id") or 0)
    data = str(callback.get("data") or "")
    chat_id = int((callback.get("message") or {}).get("chat", {}).get("id") or 0)
    if sender_id != allowed_user_id:
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                "text": "Not authorized", "show_alert": "true"})
        return
    if data.startswith("ux:"):
        _handle_ui_callback(conn, token, allowed_user_id, callback)
        return
    if not data.startswith("rv:"):
        return

    digest = hashlib.sha256(data[3:].encode("utf-8")).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id::text, review_item_id::text, action, allowed_user_id,
                      expires_at, used_at, source_sha256, context_sha256, payload_json, review_revision_id::text
                 FROM telegram_callback_tokens WHERE token_sha256 = %s FOR UPDATE;""",
            (digest,),
        )
        row = cur.fetchone()
        if not row or row[3] != sender_id or row[5] is not None:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                     "text": "Button expired or already used", "show_alert": "true"})
            return
        cur.execute("SELECT %s < now();", (row[4],))
        if cur.fetchone()[0]:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                     "text": "Button expired", "show_alert": "true"})
            return
        item_id, action = str(row[1]), str(row[2])
        token_context = str(row[7]) if row[7] else None
        callback_payload = dict(row[8] or {})
        cur.execute("SELECT source_sha256,status,item_type,current_revision_id::text FROM human_review_items WHERE id=%s;", (item_id,))
        current_item = cur.fetchone()
        current_source = str(current_item[0]) if current_item and current_item[0] else None
        token_source = str(row[6]) if row[6] else None
        token_revision = str(row[9]) if row[9] else None
        current_revision = str(current_item[3]) if current_item and current_item[3] else None
        if token_source != current_source or token_revision != current_revision:
            cur.execute("UPDATE telegram_callback_tokens SET used_at=now() WHERE id=%s;", (row[0],))
            conn.commit()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                     "text": "Review content changed; use the newest message", "show_alert": "true"})
            return
        if token_context:
            cur.execute(
                """SELECT context_sha256 FROM telegram_review_deliveries
                     WHERE review_item_id=%s AND delivery_kind='summary' AND status='sent'
                       AND context_sha256 IS NOT NULL
                     ORDER BY delivered_at DESC, id DESC LIMIT 1;""", (item_id,)
            )
            latest = cur.fetchone()
            if latest and str(latest[0]) != token_context:
                cur.execute("UPDATE telegram_callback_tokens SET used_at=now() WHERE id=%s;", (row[0],))
                conn.commit()
                api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                         "text": "Approval context changed; use the newest message",
                                                         "show_alert": "true"})
                return

    # Presentation-only actions never call decide_item and never authorize browser I/O.
    if action == "details":
        with conn.cursor() as cur:
            cur.execute("UPDATE telegram_callback_tokens SET used_at=now() WHERE id=%s;", (row[0],))
        conn.commit()
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Opening details"})
        if chat_id:
            _send_review_details(conn, token, item_id=item_id, chat_id=chat_id, context_sha=token_context)
        return
    if action == "skip":
        try:
            snooze_review_item(conn, item_id, actor=f"telegram:{sender_id}", hours=6)
            with conn.cursor() as cur:
                _clear_pending_question(cur, chat_id, item_id=item_id)
                _clear_pending_document_feedback(cur, chat_id, item_id=item_id)
                cur.execute("UPDATE telegram_callback_tokens SET used_at=now() WHERE review_item_id=%s AND used_at IS NULL;", (item_id,))
            conn.commit()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Snoozed for 6 hours"})
            if chat_id:
                dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
        except ReviewError as exc:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": str(exc)[:180], "show_alert": "true"})
        return
    if action == "document_feedback":
        try:
            _send_bound_force_reply_prompt(
                conn, token, item_id=item_id, chat_id=chat_id, source_sha256=token_source,
                callback_token_id=str(row[0]), prompt_kind="document_feedback",
                text=("✏️ Tell the document agent exactly what is wrong or what to improve. "
                      "Your feedback is editing direction, never evidence; JobOS will not invent unsupported facts. "
                      "Reply directly to this message within 15 minutes."),
                placeholder="What should the resume/cover letter agent fix?",
            )
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Reply with what to change"})
        except Exception as exc:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": str(exc)[:180], "show_alert": "true"})
        return
    if action == "other":
        try:
            _send_bound_force_reply_prompt(
                conn, token, item_id=item_id, chat_id=chat_id, source_sha256=token_source,
                callback_token_id=str(row[0]), prompt_kind="question",
                text="✏️ Reply directly to this message with your answer. This prompt expires in 15 minutes.",
                placeholder="Type your answer",
            )
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Reply to this prompt"})
        except Exception as exc:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": str(exc)[:180], "show_alert": "true"})
        return
    if action == "answer":
        answer = str(callback_payload.get("answer") or "").strip()
        scope = str(callback_payload.get("scope") or "company")
        try:
            answer_result = answer_question(conn, item_id, answer=answer, actor=f"telegram:{sender_id}", scope=scope, answer_kind="option")
            with conn.cursor() as cur:
                cur.execute("UPDATE telegram_callback_tokens SET used_at=now() WHERE review_item_id=%s AND used_at IS NULL;", (item_id,))
                _clear_pending_question(cur, chat_id, item_id=item_id)
                sync_inbox(cur)
            conn.commit()
            if answer_result.get("autofill_reprepare_required"):
                reprepare = prepare_fresh_autofill_after_human_input(str(answer_result["application_id"]))
                if not reprepare.get("ok"):
                    with conn.cursor() as cur:
                        sync_inbox(cur)
                    conn.commit()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": f"Saved: {answer}"})
            if chat_id:
                api(token, "sendMessage", data={"chat_id": str(chat_id), "text": f"✅ Saved answer: {answer}. JobOS will reuse it only within its approved scope."})
                dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
        except ReviewError as exc:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": str(exc)[:180], "show_alert": "true"})
        return
    if action == "focus_browser":
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT application_id::text,browser_task_id::text FROM human_review_items WHERE id=%s;", (item_id,))
                app_row = cur.fetchone()
                if not app_row:
                    raise ReviewError("Review item no longer exists.")
                from services.review.review_service_v1 import focus_bound_application_page
                focus_bound_application_page(cur, str(app_row[0]), browser_task_id=str(app_row[1] or "") or None)
                cur.execute("UPDATE telegram_callback_tokens SET used_at=now() WHERE id=%s;", (row[0],))
            conn.commit()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Focused JobOS browser page"})
            if chat_id:
                api(token, "sendMessage", data={"chat_id": str(chat_id), "text": "🌐 JobOS focused the exact browser page. Complete the manual step there, then use the next JobOS card."})
        except ReviewError as exc:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": str(exc)[:180], "show_alert": "true"})
        return
    if action == "sensitive_confirm":
        try:
            result = decide_item(conn, item_id, decision="approve", actor=f"telegram:{sender_id}",
                                 note="Telegram sensitive-question exact completion recheck")
            with conn.cursor() as cur:
                cur.execute("UPDATE telegram_callback_tokens SET used_at=now() WHERE review_item_id=%s AND used_at IS NULL;", (item_id,))
                _clear_pending_question(cur, chat_id, item_id=item_id)
                sync_inbox(cur)
            conn.commit()
            if result.get("autofill_reprepare_required"):
                reprepare = prepare_fresh_autofill_after_human_input(str(result["application_id"]))
                if not reprepare.get("ok"):
                    with conn.cursor() as cur:
                        sync_inbox(cur)
                    conn.commit()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Verified complete; no legal answer stored"})
            if chat_id:
                api(token, "sendMessage", data={"chat_id": str(chat_id), "text": "✅ JobOS rechecked the exact sensitive question as completed. No legal answer was stored or autofilled; a fresh plan will continue automatically."})
                dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
        except ReviewError as exc:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": str(exc)[:180], "show_alert": "true"})
        return

    approval_request_id = ""
    try:
        result = decide_item(conn, item_id, decision=action,
                             actor=f"telegram:{sender_id}", note="Telegram one-tap review decision")
        with conn.cursor() as cur:
            cur.execute("UPDATE telegram_callback_tokens SET used_at = now() WHERE review_item_id = %s AND used_at IS NULL;", (item_id,))
            _clear_pending_question(cur, chat_id, item_id=item_id)
            _clear_pending_document_feedback(cur, chat_id, item_id=item_id)
        conn.commit()  # human decision is durable before privileged browser I/O

        if result.get("autofill_reprepare_required"):
            reprepare = prepare_fresh_autofill_after_human_input(str(result["application_id"]))
            if not reprepare.get("ok"):
                result["autofill_reprepare"] = reprepare

        observed_reconciliation = result.get("reconciliation_observed_result") or {}
        followup_source = result.get("post_commit_followup_result") or observed_reconciliation
        if followup_source.get("state") and followup_source.get("state") != "submitted":
            try:
                from services.application_actions.privileged_action_v1 import _post_commit_followup
                result["post_commit_followup"] = _post_commit_followup(conn, result["application_id"], followup_source)
            except Exception as followup_exc:
                result["post_commit_followup"] = {"ok": False, "error": str(followup_exc)[:1000]}

        approval_type = str(result.get("approval_type") or "")
        approval_request_id = str(result.get("approval_request_id") or "")
        # Telegram is the human decision surface, not a second executor.  The
        # single privileged-action worker owns browser I/O after this durable
        # commit.  Calling execute_one() here raced that worker and could tell
        # the user "refused" even while the worker had already started safely.

        with conn.cursor() as cur:
            sync_inbox(cur)
        conn.commit()
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": "Done"})

        if chat_id:
            if result.get("post_commit_followup", {}).get("ok") is False:
                message = "⚠️ Decision saved, but the next gate needs a safe retry. JobOS added an actionable recovery card."
            elif approval_type == "autofill_form" and not result.get("autofill_queued"):
                message = "✅ Autofill approved. Waiting for the exact document-upload decisions; browser writes have not started yet."
            elif result.get("delegated_to_autofill") and result.get("autofill_queued"):
                message = "🚀 Upload decision saved. All exact upload gates are resolved and autofill is queued."
            elif result.get("delegated_to_autofill"):
                message = "✅ Upload decision saved. Autofill is waiting only for the remaining upload gates."
            elif result.get("materialized_approval_request_id"):
                message = "✅ Done. JobOS prepared the next exact-bound action; it is waiting in your inbox."
            elif result.get("reconciliation_outcome"):
                message = f"✅ Reconciliation recorded: {result['reconciliation_outcome']}."
            elif action == "approve" and approval_type.startswith("privileged_") and approval_request_id:
                message = "🚀 Approved. JobOS started the exact browser action in its single safe worker; this chat will receive the next decision or recovery card."
            else:
                message = "✅ Done. JobOS will continue automatically until another real decision is needed."
            api(token, "sendMessage", data={"chat_id": str(chat_id), "text": message})
            dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
    except ReviewError as exc:
        conn.rollback()
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                 "text": str(exc)[:180], "show_alert": "true"})
    except Exception as exc:
        outcome = "pre_io_refused"
        try:
            with conn.cursor() as cur:
                if approval_request_id:
                    cur.execute(
                        """SELECT status FROM privileged_action_executions
                             WHERE approval_request_id=%s ORDER BY started_at DESC LIMIT 1;""",
                        (approval_request_id,),
                    )
                    execution = cur.fetchone()
                    if execution and execution[0] == "needs_reconciliation":
                        outcome = "post_io_uncertain"
                sync_inbox(cur)
            conn.commit()
        except Exception:
            conn.rollback()
        if outcome == "post_io_uncertain":
            alert = "Browser outcome is uncertain"
            detail = "⚠️ A browser effect may have occurred. JobOS will not replay it. Use the reconciliation card in your inbox."
        else:
            alert = "Action safely refused"
            detail = "⛔ Exact binding changed, so JobOS refused the action before browser I/O. Refresh and use the new card."
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                 "text": alert, "show_alert": "true"})
        if chat_id:
            api(token, "sendMessage", data={"chat_id": str(chat_id), "text": detail})
            dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)


def handle_message(conn, token: str, allowed_user_id: int, message: dict[str, Any]) -> None:
    sender_id = int((message.get("from") or {}).get("id") or 0)
    chat_id = int((message.get("chat") or {}).get("id") or 0)
    text = str(message.get("text") or "").strip()
    if sender_id != allowed_user_id:
        return

    if text in {"/start", "/inbox", "/sync", "/status"}:
        dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
        return

    # Document-agent feedback is accepted only as a direct reply to the exact
    # short-lived prompt created from a version-bound document review card.
    with conn.cursor() as cur:
        cur.execute(
            """SELECT pending_document_review_item_id::text,pending_document_source_sha256,
                      pending_document_feedback_expires_at > now(),pending_document_prompt_message_id
                 FROM telegram_control_surface_state WHERE chat_id=%s;""", (chat_id,)
        )
        pending_doc = cur.fetchone()
    reply_to = message.get("reply_to_message") or {}
    reply_to_id = int(reply_to.get("message_id") or 0)
    if pending_doc and pending_doc[0] and text and not text.startswith("/"):
        item_id, expected_source = str(pending_doc[0]), str(pending_doc[1] or "")
        active_prompt = bool(pending_doc[2]) and int(pending_doc[3] or 0) == reply_to_id
        if not active_prompt:
            with conn.cursor() as cur:
                _clear_pending_document_feedback(cur, chat_id, item_id=item_id)
            conn.commit()
            api(token, "sendMessage", data={"chat_id": str(chat_id),
                                             "text": "That edit prompt expired or was not replied to directly. Tap ✏️ Tell agent what to fix on the current document card again."})
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT source_sha256,status,item_type FROM human_review_items WHERE id=%s;", (item_id,))
                current = cur.fetchone()
                if not current or current[2] != "document_review" or current[1] not in {"pending", "needs_revision"}:
                    raise ReviewError("That document is no longer waiting for feedback. Refresh JobOS.")
                if str(current[0] or "") != expected_source:
                    raise ReviewError("That document changed. Use the newest review card.")
            revision = submit_document_feedback(conn, item_id, feedback=text, actor=f"telegram:{sender_id}")
            with conn.cursor() as cur:
                _clear_pending_document_feedback(cur, chat_id, item_id=item_id)
                cur.execute("UPDATE telegram_callback_tokens SET used_at=now() WHERE review_item_id=%s AND used_at IS NULL;", (item_id,))
                sync_inbox(cur)
            conn.commit()
            api(token, "sendMessage", data={
                "chat_id": str(chat_id),
                "text": ("✏️ Feedback queued for the document agent. It will regenerate this exact draft, "
                         "truth-check it, and return a fresh review card automatically. Feedback is never treated as evidence."),
            })
            dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
        except ReviewError as exc:
            conn.rollback()
            with conn.cursor() as cur:
                _clear_pending_document_feedback(cur, chat_id, item_id=item_id)
            conn.commit()
            api(token, "sendMessage", data={"chat_id": str(chat_id), "text": f"⚠️ {exc}"})
        return

    # Free-text is accepted only after the user explicitly tapped ✏️ Other on a
    # particular version-bound question card. No review ID or command is needed.
    with conn.cursor() as cur:
        cur.execute(
            """SELECT pending_question_review_item_id::text,pending_question_source_sha256,
                      pending_question_expires_at > now(),pending_question_prompt_message_id
                 FROM telegram_control_surface_state WHERE chat_id=%s;""", (chat_id,)
        )
        pending = cur.fetchone()
    reply_to = message.get("reply_to_message") or {}
    reply_to_id = int(reply_to.get("message_id") or 0)
    if pending and pending[0] and text and not text.startswith("/"):
        item_id, expected_source = str(pending[0]), str(pending[1] or "")
        active_prompt = bool(pending[2]) and int(pending[3] or 0) == reply_to_id
        if not active_prompt:
            with conn.cursor() as cur:
                _clear_pending_question(cur, chat_id, item_id=item_id)
            conn.commit()
            api(token, "sendMessage", data={"chat_id": str(chat_id),
                                             "text": "That answer prompt expired or was not replied to directly. Tap ✏️ Other on the current card again."})
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT source_sha256,status FROM human_review_items WHERE id=%s;", (item_id,))
                current = cur.fetchone()
                if not current or current[1] not in {"pending", "needs_revision"}:
                    raise ReviewError("That question is no longer waiting for an answer. Refresh JobOS.")
                if str(current[0] or "") != expected_source:
                    raise ReviewError("That question changed. Use the newest card.")
            answer_result = answer_question(conn, item_id, answer=text, actor=f"telegram:{sender_id}", scope="company")
            with conn.cursor() as cur:
                _clear_pending_question(cur, chat_id, item_id=item_id)
                sync_inbox(cur)
            conn.commit()
            if answer_result.get("autofill_reprepare_required"):
                reprepare = prepare_fresh_autofill_after_human_input(str(answer_result["application_id"]))
                if not reprepare.get("ok"):
                    with conn.cursor() as cur:
                        sync_inbox(cur)
                    conn.commit()
            api(token, "sendMessage", data={"chat_id": str(chat_id),
                                             "text": "✅ Answer saved. JobOS will continue automatically and reuse it only within its approved scope."})
            dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
        except ReviewError as exc:
            conn.rollback()
            with conn.cursor() as cur:
                _clear_pending_question(cur, chat_id, item_id=item_id)
            conn.commit()
            api(token, "sendMessage", data={"chat_id": str(chat_id), "text": f"⚠️ {exc}"})
        return

    # Legacy command remains for debugging/admin, but daily UX never requires it.
    if text.startswith("/answer "):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            api(token, "sendMessage", data={"chat_id": str(chat_id), "text": "Tap ✏️ Other on the question card, then reply naturally."})
            return
        try:
            answer_result = answer_question(conn, parts[1], answer=parts[2], actor=f"telegram:{sender_id}", scope="company")
            conn.commit()
            if answer_result.get("autofill_reprepare_required"):
                prepare_fresh_autofill_after_human_input(str(answer_result["application_id"]))
            api(token, "sendMessage", data={"chat_id": str(chat_id), "text": "✅ Answer saved."})
            dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)
        except ReviewError as exc:
            conn.rollback()
            api(token, "sendMessage", data={"chat_id": str(chat_id), "text": f"⚠️ {exc}"})
        return

    if text:
        api(token, "sendMessage", data={"chat_id": str(chat_id),
                                         "text": "Use the JobOS buttons for daily work. Tap /start any time to reopen the single inbox."})


def poll_once(conn, token: str, allowed_user_id: int, *, timeout_seconds: int = 50) -> int:
    with conn.cursor() as cur:
        _reap_stale_transport(cur)
        offset = _load_offset(cur)
    conn.commit()
    payload = api(token, "getUpdates", data={"offset": str(offset),
                  "timeout": str(timeout_seconds),
                  "allowed_updates": json.dumps(["callback_query", "message"])},
                  timeout=timeout_seconds + 15)
    next_offset = offset
    updates = _result_list(payload)
    handled = 0
    for update in updates:
        update_id = _safe_int(update.get("update_id"))
        if update_id <= 0:
            continue
        update_sha = hashlib.sha256(
            json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO telegram_updates(bot_key,update_id,payload_sha256,payload_json,status)
                   VALUES (%s,%s,%s,%s,'received')
                   ON CONFLICT (bot_key,update_id) DO NOTHING
                   RETURNING status;""",
                (BOT_KEY, update_id, update_sha, Jsonb(_redact_telegram_outbox_payload(update))),
            )
            inserted = cur.fetchone()
            if inserted is None:
                cur.execute(
                    "SELECT payload_sha256,status FROM telegram_updates WHERE bot_key=%s AND update_id=%s;",
                    (BOT_KEY, update_id),
                )
                prior = cur.fetchone()
                if not prior or str(prior[0]) != update_sha:
                    conn.rollback()
                    raise TelegramError(f"Telegram update {update_id} changed bytes for an existing identity")
                if str(prior[1]) in {"processed", "processing", "uncertain"}:
                    next_offset = max(next_offset, update_id + 1)
                    continue
            cur.execute(
                """UPDATE telegram_updates SET status='processing',claimed_by='telegram-review-bot',
                          lease_expires_at=now()+interval '5 minutes',error_message=NULL
                    WHERE bot_key=%s AND update_id=%s AND status IN ('received','failed')
                    RETURNING update_id;""",
                (BOT_KEY, update_id),
            )
            if cur.fetchone() is None:
                next_offset = max(next_offset, update_id + 1)
                conn.commit()
                continue
        # Commit the durable inbox claim before callback/reply side effects.
        conn.commit()
        try:
            callback = update.get("callback_query")
            message = update.get("message")
            if isinstance(callback, dict):
                handle_callback(conn, token, allowed_user_id, callback)
            if isinstance(message, dict):
                handle_message(conn, token, allowed_user_id, message)
        except Exception as exc:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE telegram_updates SET status='uncertain',error_message=%s,lease_expires_at=NULL
                        WHERE bot_key=%s AND update_id=%s;""",
                    (str(exc)[:1000], BOT_KEY, update_id),
                )
            conn.commit()
            raise
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE telegram_updates SET status='processed',processed_at=now(),lease_expires_at=NULL
                    WHERE bot_key=%s AND update_id=%s;""",
                (BOT_KEY, update_id),
            )
            _save_offset(cur, update_id + 1)
        conn.commit()
        handled += 1
        next_offset = max(next_offset, update_id + 1)
    if next_offset != offset:
        with conn.cursor() as cur:
            _save_offset(cur, next_offset)
        conn.commit()
    return handled


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS Telegram review adapter")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dispatch-only", action="store_true")
    parser.add_argument("--discover-id", action="store_true")
    # Browser state advances independently; avoid hiding a completed login
    # behind Telegram's former fifty-second long-poll cadence.
    parser.add_argument("--poll-timeout", type=int, default=5)
    args = parser.parse_args()
    if args.discover_id:
        token = _bot_token()
        me = api(token, "getMe", data={})
        print(f"Telegram bot: @{_result_dict(me).get('username', 'unknown')}")
        rows = discover_ids(token)
        print(json.dumps(rows, indent=2))
        if not rows:
            print("No recent messages. Send /start to the bot, then run again.")
        return 0
    token, allowed_user_id, chat_id = _required_env()
    me = api(token, "getMe", data={})
    print(f"Telegram bot: @{_result_dict(me).get('username', 'unknown')} | chat={chat_id} | allowed_user={allowed_user_id}")
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        while True:
            dashboard = dispatch_dashboard(conn, token, allowed_user_id, chat_id)
            urgent = dispatch_pending(conn, token, allowed_user_id, chat_id, limit=3, urgent_only=True)
            if dashboard or urgent:
                print(f"Control surface updated; urgent cards delivered={urgent}.")
            if args.dispatch_only:
                return 0
            updates = poll_once(conn, token, allowed_user_id,
                                timeout_seconds=max(1, min(args.poll_timeout, 10)))
            if args.once:
                return 0
            if not dashboard and not urgent and not updates:
                time.sleep(1)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TelegramError, ReviewError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
